#!/usr/bin/env python3
"""Issue #6866 re-validation on pinned v0.113.0 (protocol 151): Delina, Wild Mage.

Reported (Discord): "[[delina, wild mage]] doesn't reroll on 15-20"

Oracle (pinned card-data): "Whenever Delina attacks, choose target creature
you control, then roll a d20. 1-14 | Create a tapped and attacking token that's
a copy of that creature, except it's not legendary and it has 'At end of
combat, exile this token.' 15-20 | Create one of those tokens. You may roll
again."

Prior runs: v0.80.0 (20260911-6866b, protocol 69) reproduced; v0.98.0
(20261001-6866, protocol 94) reproduced: low turn created exactly 1 token
(1-14 branch); high turn (trigger fired, 0 tokens) offered no "roll again"
prompt; the low-turn token was also NOT exiled by PostCombatMain (A7 failed).
v0.102.0 (20261006-6866a, protocol 106) reproduced: 15-20 still creates no
token, the engine now surfaces the "you may roll again" prompt but accepting
it resolves nothing, and the low-turn token is still not exiled. v0.107.0
(run-6866-reval-v01070-20261011-1611, protocol 130) reproduced: A5/A6/A7
failed (reroll offered but accepting resolves nothing; token not exiled).
v0.110.0
(run-6866-reval-v01100-20261015-2041, protocol 142) reproduced: A6
reroll_works FAILED (accepted "roll again" resolved nothing); A7 exile_ok
FAILED (turn-15 token still on BF post-combat); 15-20 branch still parses
Unimplemented on the v0.110.0 card data.
This run re-validates on v0.113.0/protocol 151.
This 01100 port was a mechanical port of scenario_6866_01070.py (verified
2026-10-11 on v0.107.0/protocol 130) with all protocol-142 lessons applied.
This 01130 port is a mechanical port of scenario_6866_01100.py (verified
2026-10-15 on v0.110.0/protocol 142); pin bumps only, driver logic unchanged
(interaction conventions proven identical 142->151 by the #6860/#6861/#6862
v0.113.0 runs on 2026-10-18, same driver family). HELLO advertises 151
(exact match); engine auto-pays mana (bare CastSpell,
no tapLandForMana bookkeeping); cast-confirmation guard after EVERY
CastSpell submission (never pass priority or start new main-phase plays
until the spell is confirmed on stack/battlefield -- set on CONFIRMATION,
never on submission; 30s backstop clears a dropped cast so the state-based
cast block re-triggers and retries); chooseBranch matched on the
chooseBranch action code; repeating prompts answered per-firing keyed by
interactionId; export response data.state is a JSON string parsed once.
The 15-20 branch parsed on v0.110.0 as Unimplemented (re-checked live
against the v0.113.0 card data by this run)
("unparsed_verb_arguments", "Create one of those tokens") with an optional
sub_ability=Unimplemented("roll again"). The die-roll value is not exposed
to the client, so 15-20 is identified by its effect signature (trigger fired,
0 new tokens) while 1-14 provably creates exactly one token.

Plan (two human seats, native engine, v0.113.0 / protocol 151):
  SETUP  - P0: dense deck (8x Delina, 12x Grizzly Bears, 20x Mountain,
           20x Forest); develops lands, casts Bear, casts Delina.
           P1: passive (30x Forest + 30x Llanowar Elves); develops lands and
           casts Elves to chump-block (Delina 3/2 vs Elf 1/1: elf dies,
           Delina lives, P1 takes 0).
  ATTACK - P0 attacks with Delina alone every turn (relations-schema
           declare via vi opportunity); the trigger's target prompt is
           answered with the Bear (fallback: Delina herself when no Bear is
           out). Per attack turn record: target answered (turn), new P0
           token oids after trigger resolution (delta 1 => 1-14, delta 0 =>
           15-20 signature), vi opportunity shapes seen. P1 blocks every
           attacker with a separate untapped Elf (relations-schema).
  STOP   - once >=1 low turn (delta==1, control) and >=1 high turn
           (trigger answered, initial delta==0) are recorded and closing
           exports are done; watchdog 14 attack turns.

Behavioral contract:
  A1 setup_ok       pre.json: P0 PreCombatMain, Delina on BF (Bear on BF
                    preferred for the 2-target prompt path), life 20/20
  A2 trigger_fires  >=1 Delina trigger target prompt answered (Bear or Delina)
  A3 low_branch     >=1 turn with exactly 1 new token: tapped, attacking,
                    is_token, a copy of the targeted Bear, non-legendary
  A4 high_observed  >=1 turn where the trigger fired and the initial roll
                    resolved with 0 new tokens (15-20 signature; 1-14
                    provably creates a token per A3)
  A5 reroll_offered on a high turn the engine offered the optional
                    "you may roll again" prompt (decideOptionalEffect)
  A6 reroll_works  accepting the reroll produced a further roll resolution
                    (a new token from a 1-14 re-roll, or a repeated prompt
                    from another 15-20)
  A7 exile_ok       on low turns the token is exiled by PostCombatMain
                    (acceptance criterion; recorded as observed)

Verdict = reproduced iff A1..A4 pass and the reroll does not actually happen
(A5 fails: never offered -- the original report; or A5 passes but A6 fails:
offered yet accepting resolves nothing). not-reproduced iff A1..A6 all pass.
Blocked if no Delina attack turn completes.

Protocol-151 driver (v0.113.0, ported from the verified v0.110.0
scenario_6866_01100.py; conventions proven identical 142->151):
  - HELLO advertises protocol 151 (exact match); CreateGameWithSettings +
    JoinGameWithPassword + start_when_full; gate on viewer_interaction
    (waiting_for is gone/null on 142); export response data.state is a JSON
    string parsed once.
  - MulliganDecision answered via legacy Action, gated on the advertised
    action; BottomCards/DiscardToHandSize via vi schema/select.
  - CastSpell via advertised actions as-is; mana is engine auto-tapped
    (manual taps disabled except as a >90s last-resort fallback).
  - playLand is in NON_DECISION_CODES (bundled into the ordinary priority
    action menu; playing a land is optional so the menu is never a forced
    decision); explicit land-play (advertised PlayLand, vi playLand
    fallback) runs before the decision gate. Land matching uses is_land().
  - surf_codes() filters None codes (142 surfaces sometimes carry
    "data": {} with no "code").
  - Delina's trigger target prompt: vi schema/exactChoices whose candidates
    reference BF creatures controlled by seat 0; answer Bear first, then
    Delina herself.
  - Reroll: decideOptionalEffect opportunity (exactChoices bare true/false
    with ("value","accept","true"|"false") surfaces, or schema); the FIRST
    prompt of the run is accepted (the reported branch), later ones declined
    (control).
  - DeclareAttackers/Blockers: relations-schema vi opportunity (spec type
    "relations"), declared-phase gated; advertised-action first for empty
    declares, then vi, then blind submit.
  - Priority passes only when the seat genuinely holds it (PassPriority in
    top-level legal_actions); per-revision pass guards; 5s re-tick backstop.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
import traceback

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6866
RUN_ID = "run-6866-reval-v01130-20261018-1311"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
TMPD = f"/tmp/ev6866_{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(TMPD, exist_ok=True)
# Multi-game attempts: GAME_INDEX=0 starts a fresh run; >0 continues it
# (fresh RNG seed per game). Progress persists in run_progress.json.
GINDEX = int(os.environ.get("GAME_INDEX", "0"))
if GINDEX == 0:
    assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
    WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
else:
    WIRE = open(f"{EVDIR}/wire_log.jsonl", "a")
    RUNLOG = open(f"{EVDIR}/scenario_run.log", "a")

GAME_TIMEOUT = 2400
GAME_ATTACK_CAP = 8      # attacks per game without a high turn -> new game
STALL_AFTER = 150        # seconds without a new revision -> game wedged
MAX_GAMES = 4

ST = {
    "step": "SETUP", "stage": "SETUP", "stop": False,
    "attack_turns": [], "tokens_before": {}, "deltas": {},
    "target_answers": [], "low_turn": None, "high_turn": None,
    "low_ref": None, "high_ref": None,   # (gindex, turn) of decisive turns
    "reroll_prompts": [], "reroll_answers": [], "tokens_at_reroll": {},
    "pre_exported": False,
    "mid_low_exported": False, "post_low_exported": False,
    "mid_high_exported": False, "post_high_exported": False,
    "exile_check": {}, "delina_turn": None,
    "game_code": None, "legend_answered": 0,
    "mulls": {}, "wf_seen": [], "opp_shapes_logged": set(),
    "games": [],            # per-game summaries across attempts
    "game_result": None,
}
PROGRESS_PATH = f"{EVDIR}/run_progress.json"
if GINDEX > 0 and os.path.exists(PROGRESS_PATH):
    _prog = json.load(open(PROGRESS_PATH))
    for _k in ("games", "low_turn", "high_turn", "low_ref", "high_ref",
               "target_answers", "reroll_prompts", "reroll_answers",
               "pre_exported", "mid_low_exported", "post_low_exported",
               "mid_high_exported", "post_high_exported"):
        if _k in _prog:
            ST[_k] = _prog[_k]
    print(f"resumed progress from {GINDEX} prior game(s): "
          f"low={ST['low_turn']} high={ST['high_turn']}", flush=True)


def save_progress():
    prog = {k: ST[k] for k in (
        "games", "low_turn", "high_turn", "low_ref", "high_ref",
        "target_answers", "reroll_prompts", "reroll_answers",
        "pre_exported", "mid_low_exported", "post_low_exported",
        "mid_high_exported", "post_high_exported")}
    with open(PROGRESS_PATH, "w") as f:
        json.dump(prog, f, indent=1)
SUBMITTED_OPPS = set()
# Cast-confirmation guard (protocol 151): after EVERY CastSpell submission,
# the driver must not pass priority or start new main-phase plays until the
# spell is confirmed on stack/battlefield -- a bare CastSpell (payment_mode
# Auto) can LOSE A RACE to the driver's own PassPriority submitted on the
# next tick, and the server sends NO rejection for the dropped cast.
# Confirmation is checked per tick (cast_zone of the submitted oid);
# a 30s backstop clears a still-in-hand cast as dropped so the state-based
# cast block re-triggers and retries. Keyed by client name so each seat's
# guard is independent.
CAST_PENDING = {}


def cast_zone(state, oid):
    return str(get_obj(state, oid).get("zone") or "").lower()


def note_cast_pending(c, kind, oid, name):
    CAST_PENDING[c.name] = {"kind": kind, "oid": str(oid), "name": name,
                            "since": time.time()}


async def guard_cast_pending(c, tag, st):
    """Runs BEFORE any early-return block. Returns "waiting" while the
    client's CastSpell is unconfirmed (caller must hold and return),
    "confirmed"/"dropped"/None otherwise."""
    pend = CAST_PENDING.get(c.name)
    if not pend:
        return None
    state = (st or {}).get("state") or {}
    oid = pend["oid"]
    zone = cast_zone(state, oid)
    if zone in ("stack", "battlefield"):
        CAST_PENDING.pop(c.name, None)
        # also clear the legacy ST["cast"] in-flight marker for this oid
        cs = ST.get("cast")
        if cs and str(cs.get("oid")) == oid:
            cs["in_flight"] = False
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} (oid {oid})")
        wire("cast_confirmed", {"kind": pend["kind"], "oid": oid,
                                "zone": zone})
        return "confirmed"
    if time.time() - pend["since"] > 30:
        CAST_PENDING.pop(c.name, None)
        say(f"[{tag}] cast NOT confirmed after 30s (zone={zone}): "
            f"{pend['name']} (oid {oid}); will retry")
        wire("cast_dropped_retry", {"kind": pend["kind"], "oid": oid,
                                    "zone": zone})
        return "dropped"
    return "waiting"
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.113.0",
    "build_commit": "0571ee3",
    "protocol_version": 151,
    "server_binary_sha256": "f2b09aab4385ff6e6f352d211c2560af42445e11e6b5f2fef0065094c1a8c77d",
    "card_data_sha256": "eb309108eb0df708b6cb5b3c75858149a9fd2864ee0f4ef81195cc75af058b85",
    "draft_pools_sha256": "785376ea4dc8fd2fde29e734a1b069de3a12c57b2df1bb1471556b279b306400",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.113.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.113.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.113.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.113.0/data/card-data.json"))

DELINA = "Delina, Wild Mage"
BEAR = "Grizzly Bears"
ELF = "Llanowar Elves"
MTN = "Mountain"
FOREST = "Forest"
LANDS = (MTN, FOREST)

P0_DECK = [(DELINA, 8), (BEAR, 12), (MTN, 20), (FOREST, 20)]
P1_DECK = [(FOREST, 30), (ELF, 30)]

# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {}) or {}


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_obj(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_obj(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def find_bf_oid(state, pid, lname):
    oids = bf_by_name(state, pid, lname)
    return oids[0] if oids else None


def find_hand(state, pid, name):
    for o in hand_ids(state, pid):
        if oname(get_obj(state, o)) == name:
            return o
    return None


def p0_token_oids(state):
    return {str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == 0
            and o.get("is_token")}


def combat_attacker_oids(state):
    return {str(a.get("object_id"))
            for a in ((state.get("combat") or {}).get("attackers") or [])
            if a.get("object_id") is not None}


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid, lname=None):
    return sum(1 for oid in bf_oids(state, pid)
               if not get_obj(state, oid).get("tapped")
               and (lname is None or obj_lname(state, oid) == lname))


def pool_len(state, pid):
    p = player_obj(state, pid)
    return len(list((p.get("mana_pool") or {}).get("mana") or []))


def life_of(state, pid):
    return player_obj(state, pid).get("life")


def object_on_stack_or_bf(state, lname):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == lname \
                and o.get("zone") in ("Stack", "Battlefield"):
            return True
    return False


def delina_stack(state):
    """Delina trigger/ability objects currently on the stack."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Stack" and "delina" in oname(o).lower():
            out.append(str(oid))
    return out


# ------------------------------------------------------------- vi helpers
def top_acts(st):
    return list(st.get("legal_actions", []) or [])


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def opp_codes(opp):
    codes = set()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    for ch in (data.get("candidates") or data.get("choices") or []):
        codes.update(c for c in surf_codes(ch) if c)
    return codes


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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


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


def st_of(c):
    return c.latest or {}


def setup_gate(state):
    delina = find_bf_oid(state, 0, DELINA.lower())
    bear = find_bf_oid(state, 0, BEAR.lower())
    return (delina is not None and bear is not None
            and my_main(state, 0)
            and life_of(state, 0) == 20 and life_of(state, 1) == 20)


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.113.0"), f"unexpected version {ver}"
    assert int(proto) == 151, f"unexpected protocol {proto}"
    assert str(build) == "0571ee3", f"unexpected build {build}"


def check_data_level():
    """Record how Delina parses in the pinned v0.113.0 card data: is the
    15-20 branch (create token + optional roll again) implemented, or still
    Unimplemented as on v0.98.0?"""
    e = CARD_DATA.get("delina, wild mage", {})
    oracle = str(e.get("oracle_text", ""))
    trig = e.get("triggered_abilities") or e.get("triggers") or []
    blob = json.dumps(trig).lower()
    finding = ("Delina parse: " + blob[:600]) if trig else \
        "Delina has no triggered_abilities entry in pinned card data"
    unimpl = ("unimplemented" in blob
              and ("roll again" in blob or "roll_again" in blob
                   or "reroll" in blob))
    note = ("15-20 branch still parses Unimplemented (create + optional "
            "roll again)" if unimpl else
            "15-20 branch parse changed vs v0.98.0; see data_evidence.json")
    say(f"data-level finding: {note}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"card": "Delina, Wild Mage", "oracle_text": oracle,
                   "triggered_abilities": trig, "finding": note}, f, indent=1)
    return note


# ------------------------------------------------------------- common ticks
async def do_bottom(c, acts, st, pid, tag):
    """Protocol 142: bottom-after-mulligan surfaces as a vi schema/select
    opportunity, gated on waitingForKind.code == 'mulligan' at turn 1 /
    Untap. Bottoms the N lowest-priority cards (never Delina/Bear/Elf)."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
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
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        keep = {DELINA.lower(), BEAR.lower(), ELF.lower()}

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in keep:
                return (2, str(ref))
            if is_land(get_obj(state, ref)) if ref is not None else False:
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = ST["mulls"].get(tag, 0)
    if tag == "P0":
        choice = "Keep" if (DELINA.lower() in hand or BEAR.lower() in hand
                            or n >= 2) else "Mulligan"
    else:
        choice = "Keep"
    if choice == "Mulligan":
        ST["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (DELINA.lower(), BEAR.lower()):
        return 3
    if is_land(get_obj(state, o)):
        return 2
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln == ELF.lower():
        return 3
    if is_land(get_obj(state, o)):
        return 2
    return 0


async def do_discard(c, acts, st, pid, tag, rank):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand, key=lambda o: (rank(state, o), obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def do_declare_empty(c, acts, st, pid, tag, attackers_ok=True):
    """Declare empty attackers/blockers. attackers_ok=False (P0's own
    DeclareAttackers) means: never touch attacker declares here -- the
    dedicated Delina handler owns them. The vi fallback only answers
    relations-schema declare opportunities (select/sequence schemas are
    decisions like target selection, never declares)."""
    for a in acts:
        if a.get("type") == "DeclareAttackers" and attackers_ok:
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            if tag == "P1" and time.time() < ST.get("p1_block_grace_until", 0):
                continue  # relations blockers opp may still arrive; wait
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    # 142 fallback: relations-schema vi opportunity, then blind submit
    # (declare-phase gated; attackers only when attackers_ok)
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower() \
            and attackers_ok:
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            spec = (resp.get("data") or {}).get("spec", {}) or {}
            if spec.get("type") != "relations":
                continue
            iid = opp.get("interactionId")
            if (tag, "declare-atk", str(iid)) in SUBMITTED_OPPS \
                    or (tag, "declare-blk", str(iid)) in SUBMITTED_OPPS:
                return True
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi relations opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
        key = (tag, "declare-blind", str(c.revision))
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] blind declare empty (142 shape)")
            wire("declare_empty_blind", {"who": tag})
            await c.send_action({"type": "DeclareAttackers",
                                 "data": {"attacks": [], "bands": []}})
            return True
    return False


async def do_legend(c, acts, st, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            ST["legend_answered"] += 1
            say(f"[{tag}] legend rule: keeps first (advertised action)")
            wire("legend", {"who": tag, "style": "action"})
            return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        if not chs:
            continue
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in chs)).lower()
        if "legend" not in blob:
            continue
        iid = opp.get("interactionId")
        key = (tag, "legend", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        ST["legend_answered"] += 1
        say(f"[{tag}] legend rule: keeps first (vi choice)")
        wire("legend", {"who": tag, "style": "vi"})
        await answer_vi(c, opp, chs[0], tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    """Tap one land via the 142 tapLandForMana menu. One tap per call."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
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
        key = (tag, "tap", str(iid))
        if key in SUBMITTED_OPPS:
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
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] tap land for mana used_for={used} "
            f"(remaining needs={dict(needs)})")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
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
    # 142 may also advertise land plays as a vi playLand opportunity
    st = st_of(c)
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


async def pass_priority(c, st, acts, tag):
    for a in acts:
        if a.get("type") == "PassPriority":
            key = (tag, "pass", str(c.revision))
            if key in PASSED_REV:
                return True
            PASSED_REV[key] = True
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
                key = (tag, "pass", str(iid))
                if key in PASSED_REV:
                    return True
                PASSED_REV[key] = True
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  tag)
                return True
    return False


def find_cast_action(acts, state, lname, oid=None):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if oid is not None and str(iv) != str(oid):
                continue
            if obj_lname(state, iv) == lname:
                return a
    return None

# ------------------------------------------------------------- Delina combat
def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


def relations_shape(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    return edges, cands


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    return None


async def do_declare_attackers_delina(c, acts, st, pid, tag):
    """P0 declares Delina alone via the relations-schema opportunity (142
    analog of the protocol-94 relations declare)."""
    state = st["state"]
    if str(state.get("active_player")) != str(pid):
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    edges, cands = relations_shape(opp)
    if "attack_edges" not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add("attack_edges")
        wire("declare_attackers_shape",
             {"n_edges": len(edges), "edges": edges[:6],
              "n_candidates": len(cands)})
    delina_oid = find_bf_oid(state, pid, DELINA.lower())
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        cand = cands.get(src, {})
        ref = cand_object_ref(cand)
        if ref is None or str(ref) != str(delina_oid):
            continue
        o = get_obj(state, ref)
        if o.get("tapped") or o.get("summoning_sick") \
                or o.get("has_summoning_sickness"):
            continue
        want_tid = None
        for tid in tids:
            if candidate_seat(cands.get(tid, {})) == 1:
                want_tid = tid
                break
        if want_tid is None and tids:
            want_tid = tids[0]
        if want_tid:
            rels.append({"sourceId": src, "targetId": want_tid,
                         "group": None})
    # Delina unable to attack -> declare empty (keeps the game moving)
    if not rels:
        sub = {"interactionId": iid,
               "response": {"type": "relations", "data": {"relations": []}}}
        wire("declare_attackers", {"who": tag, "n_rels": 0,
                                  "reason": "delina-unable",
                                  "submission": sub})
        say(f"[{tag}] declares no attackers (Delina unable, turn "
            f"{state.get('turn_number')})")
        SUBMITTED_OPPS.add(key)
        await interact_as(c, sub, tag)
        return True
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare_attackers", {"who": tag, "n_rels": len(rels),
                              "submission": sub})
    say(f"[{tag}] declares attackers: Delina alone (turn "
        f"{state.get('turn_number')})")
    SUBMITTED_OPPS.add(key)
    await interact_as(c, sub, tag)
    return True


async def do_declare_blockers_p1(c, acts, st, pid, tag):
    """P1 blocks every advertised attacker with a separate untapped Elf
    (greedy 1:1, Delina first). If the relations opportunity hasn't
    arrived yet, arm a grace window and wait -- the caller must not
    submit the advertised empty DeclareBlockers meanwhile."""
    state = st["state"]
    if str(state.get("active_player")) == str(pid):
        return False
    if "declareblock" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is None:
        attackers = combat_attacker_oids(state)
        if attackers:
            ST["p1_block_grace_until"] = time.time() + 25
            wire("block_grace", {"until": ST["p1_block_grace_until"],
                                 "attackers": len(attackers)})
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-blk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    edges, cands = relations_shape(opp)
    if "block_edges" not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add("block_edges")
        wire("declare_blockers_shape",
             {"n_edges": len(edges), "edges": edges[:8]})

    def cand_ref_name(cid):
        ref = cand_object_ref(cands.get(cid, {}))
        o = get_obj(state, ref) if ref else {}
        return oname(o).lower(), ref, o

    attackers = []
    seen = set()
    for e in edges:
        for tid in e.get("targetIds") or []:
            if tid not in seen:
                seen.add(tid)
                attackers.append(tid)
    attackers.sort(key=lambda tid: 0 if cand_ref_name(tid)[0] == DELINA.lower() else 1)
    used = set()
    rels = []
    for tid in attackers:
        for e in edges:
            src = e.get("sourceId")
            if (e.get("targetIds") or []) and tid not in (e.get("targetIds") or []):
                continue
            nm, ref, o = cand_ref_name(src)
            if nm != ELF.lower() or not ref or ref in used:
                continue
            if o.get("tapped"):
                continue
            rels.append({"sourceId": src, "targetId": tid, "group": None})
            used.add(ref)
            break
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare_blockers", {"who": tag, "n_rels": len(rels),
                             "attackers": len(attackers),
                             "submission": sub})
    say(f"[{tag}] declares blockers: {len(rels)} relations for "
        f"{len(attackers)} attackers")
    SUBMITTED_OPPS.add(key)
    await interact_as(c, sub, tag)
    return True


def _is_delina_target_opp(opp, state):
    """Delina's 'choose target creature you control' prompt: candidates
    reference BF creatures controlled by seat 0 (no player/seat targets)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    items = data.get("candidates") or data.get("choices") or []
    if not items:
        return None
    if opp_codes(opp) & {"decideOptionalEffect", "decideOptionalCost"}:
        return None
    if rtype == "schema":
        spec = (data.get("spec") or {}).get("type") or ""
        if spec not in ("select", "sequence"):
            return None
    refs = []
    for ch in items:
        if candidate_seat(ch) is not None:
            return None  # player targets -> not Delina's trigger
        ref = _cand_reference(ch)
        if ref is None:
            return None
        o = get_obj(state, ref)
        if o.get("zone") != "Battlefield" \
                or str(o.get("controller", -1)) != "0" \
                or is_land(o):
            return None
        refs.append((ch, ref))
    return refs or None


async def answer_delina_target(c, st, tag):
    """Answer Delina's attack-trigger target prompt: our Bear (fallback:
    Delina herself)."""
    state = st["state"]
    turn = state.get("turn_number")
    for opp in vi_ops(st):
        refs = _is_delina_target_opp(opp, state)
        if not refs:
            continue
        iid = opp.get("interactionId")
        key = (tag, "delina-target", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        want = None
        for ch, ref in refs:
            if obj_lname(state, ref) == BEAR.lower():
                want = (ch, ref)
                break
        if want is None:
            for ch, ref in refs:
                if obj_lname(state, ref) == DELINA.lower():
                    want = (ch, ref)
                    break
        if want is None:
            want = refs[0]
        ch, ref = want
        SUBMITTED_OPPS.add(key)
        ST["target_answers"].append((turn, str(ref), obj_lname(state, ref)))
        say(f"[{tag}] Delina trigger targets {obj_lname(state, ref)} "
            f"(turn {turn})")
        wire("delina_target_answer",
             {"turn": turn, "ref": str(ref),
              "name": obj_lname(state, ref),
              "n_candidates": len(refs)})
        await answer_vi(c, opp, ch, tag)
        return True
    # diagnostic: log the shapes we saw but didn't match (once per rev,
    # only in combat phases where a trigger prompt is plausible)
    dkey = (tag, "tgt-miss", str(c.revision))
    if dkey not in SUBMITTED_OPPS and vi_ops(st) \
            and str(state.get("phase") or "") in (
                "DeclareAttackers", "DeclareBlockers", "CombatDamage"):
        SUBMITTED_OPPS.add(dkey)
        summ = []
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            items = data.get("candidates") or data.get("choices") or []
            summ.append({
                "iid": opp.get("interactionId"),
                "rtype": resp.get("type"),
                "spec": ((data.get("spec") or {}).get("type")),
                "n": len(items),
                "codes": sorted(opp_codes(opp)),
                "desc": str(opp.get("description", ""))[:120],
            })
        wire("delina_target_miss", {"turn": turn, "opps": summ})
    return False


def pick_bool_choice(choices, want):
    """Pick the accept/decline choice from bare true/false 142 surfaces."""
    want_s = "true" if want else "false"
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if s.get("type") == "value" and dd.get("role") == "accept" \
                    and str(dd.get("value")).lower() == want_s:
                return ch
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if dd.get("role") in ("pay", "accept", "decision"):
                v = str(dd.get("value")).lower()
                if (want and v == "true") or (not want and v == "false"):
                    return ch
    for ch in choices:
        t = choice_text(ch).lower()
        if want and any(k in t for k in ("pay", "accept", "yes", "sacrifice")) \
                and "don't" not in t and "decline" not in t and "not " not in t:
            return ch
        if not want and any(k in t for k in ("decline", "don't", "do not")):
            return ch
    for ch in choices:
        if choice_text(ch).strip().lower() == want_s:
            return ch
    return None


async def answer_delina_reroll(c, st, tag):
    """The 15-20 'you may roll again' prompt: decideOptionalEffect
    opportunity for P0. The FIRST prompt of the run is accepted (the
    reported branch); later ones are declined (control)."""
    state = st["state"]
    turn = state.get("turn_number")
    for opp in vi_ops(st):
        codes = opp_codes(opp)
        if "decideOptionalEffect" not in codes:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        if not choices:
            continue
        iid = opp.get("interactionId")
        key = (tag, "delina-reroll", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        want_accept = len(ST["reroll_prompts"]) == 0
        ch = pick_bool_choice(choices, want_accept)
        if ch is None:
            say(f"[{tag}] reroll prompt: no {'accept' if want_accept else 'decline'} "
                f"choice found; deferring")
            wire("reroll_unmatched", {"opp": opp})
            return True
        SUBMITTED_OPPS.add(key)
        ST["reroll_prompts"].append((turn, want_accept))
        ST["reroll_answers"].append((turn, want_accept, choice_text(ch)[:60]))
        ST["tokens_at_reroll"][turn] = sorted(p0_token_oids(state))
        say(f"[{tag}] 'roll again' prompt (turn {turn}): "
            f"{'ACCEPTED' if want_accept else 'DECLINED'}")
        wire("delina_reroll_answer",
             {"turn": turn, "accept": want_accept,
              "tokens_before": ST["tokens_at_reroll"][turn]})
        await answer_vi(c, opp, ch, tag)
        return True
    return False


async def drive_mana(c, tag):
    """Mana for in-flight Bear/Delina casts. The pinned engine auto-taps
    reliably, so manual tapping is DISABLED by default: tapping alongside
    the auto-tap overpays into the pool. Manual taps engage only as a
    last resort if a cast sits unannounced for >90s with a tap menu
    visible (logged loudly)."""
    cs = ST.get("cast")
    st = st_of(c)
    state = st["state"]
    if cs and cs["in_flight"]:
        if object_on_stack_or_bf(state, cs["lname"]):
            cs["in_flight"] = False
            return False
        if time.time() - cs["submit_t"] < 90:
            return False
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) <= 0:
            return False
        if await pay_mana_vi(c, st, tag, needs):
            cs["taps"] += 1
            say(f"[{tag}] FALLBACK manual tap #{cs['taps']} for {cs['tag']} "
                f"(auto-tap did not fire in 90s)")
            wire("manual_tap", {"tag": cs["tag"], "taps": cs["taps"],
                                "needs": dict(needs), "fallback": True})
            return True
        return False
    return False


async def p0_develop(c, state, acts, tag):
    """Main-phase development: land drop, then Bear, then Delina."""
    if await play_a_land(c, state, 0, acts, tag):
        return True
    if find_bf_oid(state, 0, BEAR.lower()) is None:
        oid = find_hand(state, 0, BEAR)
        if oid:
            a = find_cast_action(acts, state, BEAR.lower(), oid)
            if a is not None:
                ST["cast"] = {"tag": f"bear-{oid}", "oid": str(oid),
                              "lname": BEAR.lower(),
                              "in_flight": True, "taps": 0,
                              "submit_t": time.time()}
                MANA_NEEDS[tag] = {"G": 1, "generic": 1}
                say(f"[{tag}] casts Grizzly Bears ({oid})")
                wire("cast_submit", {"tag": "bear", "oid": oid})
                await submit_as_is(c, a)
                note_cast_pending(c, "bear", oid, "Grizzly Bears")
                return True
    if find_bf_oid(state, 0, DELINA.lower()) is None:
        oid = find_hand(state, 0, DELINA)
        if oid:
            a = find_cast_action(acts, state, DELINA.lower(), oid)
            if a is not None:
                ST["cast"] = {"tag": f"delina-{oid}", "oid": str(oid),
                              "lname": DELINA.lower(),
                              "in_flight": True, "taps": 0,
                              "submit_t": time.time()}
                MANA_NEEDS[tag] = {"R": 1, "generic": 3}
                say(f"[{tag}] casts Delina, Wild Mage ({oid})")
                wire("cast_submit", {"tag": "delina", "oid": oid})
                await submit_as_is(c, a)
                note_cast_pending(c, "delina", oid, "Delina, Wild Mage")
                return True
    return False


async def p1_develop(c, state, acts, tag):
    """P1: land drop, then cast Elves (blockers)."""
    if await play_a_land(c, state, 1, acts, tag):
        return True
    oid = find_hand(state, 1, ELF)
    if oid:
        a = find_cast_action(acts, state, ELF.lower(), oid)
        if a is not None:
            MANA_NEEDS[tag] = {"G": 1}
            say(f"[{tag}] casts Llanowar Elves ({oid})")
            wire("cast_submit", {"tag": "elf", "oid": oid})
            await submit_as_is(c, a)
            note_cast_pending(c, "elf", oid, "Llanowar Elves")
            return True
    return False


# ------------------------------------------------------------- seat ticks
async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    # cast-confirmation guard (protocol 151) runs BEFORE any early-return
    # block: hold the whole tick while our CastSpell is unconfirmed.
    if await guard_cast_pending(c, tag, st) == "waiting":
        return True
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await pay_tick(c, acts):
        return True
    if await do_declare_attackers_delina(c, acts, st, 0, tag):
        return True
    # P0 never blocks; empty blockers only. attackers_ok=False so this
    # never eats P0's own attacker declare or a target prompt.
    if await do_declare_empty(c, acts, st, 0, tag, attackers_ok=False):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    # Delina trigger decisions run before main-phase logic and the
    # decision gate so they are never starved.
    if await answer_delina_target(c, st, tag):
        return True
    if await answer_delina_reroll(c, st, tag):
        return True
    if await drive_mana(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await p0_develop(c, state, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        await pass_priority(c, st, top_acts(st), tag)
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    # cast-confirmation guard (protocol 151) runs BEFORE any early-return
    # block: hold the whole tick while our CastSpell is unconfirmed.
    if await guard_cast_pending(c, tag, st) == "waiting":
        return True
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await pay_tick(c, acts):
        return True
    # blockers via relations opportunity FIRST: do_declare_empty would
    # otherwise eat the advertised DeclareBlockers before the vi
    # opportunity arrives (grace window in do_declare_blockers_p1).
    if await do_declare_blockers_p1(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await p1_develop(c, state, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        await pass_priority(c, st, top_acts(st), tag)
        return True
    return False


async def do_export(c, path, dest_dir=None):
    s = await c.export_state()
    with open(f"{dest_dir or EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]

# ------------------------------------------------------------- flow tracking
async def evaluate_flow(p0, obs):
    st = st_of(p0)
    if not st:
        return
    state = st["state"]
    turn = state.get("turn_number")
    phase = state.get("phase")
    active = state.get("active_player")

    if ST["stage"] == "SETUP":
        delina_bf = find_bf_oid(state, 0, DELINA.lower()) is not None
        bear_bf = find_bf_oid(state, 0, BEAR.lower()) is not None
        if delina_bf and ST.get("delina_turn") is None:
            ST["delina_turn"] = turn
            say(f"Delina on BF at turn {turn}")
        # need Delina + (ideally) Bear for the 2-target prompt path;
        # fall back to Delina-alone after 6 turns so a slow Bear draw
        # can't stall setup forever
        if my_main(state, 0) and delina_bf and (
                bear_bf or (turn - (ST.get("delina_turn") or turn)) >= 6):
            await do_export(p0, "pre.json")
            ST["pre_exported"] = True
            ST["stage"] = "ATTACK"
            say(f"=== stage -> ATTACK (bear_bf={bear_bf}) ===")
            wire("stage_attack", {"bear_bf": bear_bf})
        return

    if ST["stage"] != "ATTACK":
        return

    # per-turn pre-attack snapshot (kept in /tmp; the decisive turns are
    # copied into the evidence dir at the end)
    if phase == "PreCombatMain" and active == 0 \
            and turn not in ST["attack_turns"] \
            and find_bf_oid(state, 0, DELINA.lower()) is not None:
        ST["attack_turns"].append(turn)
        try:
            await do_export(p0, f"pre_attack_g{GINDEX}_{turn}.json",
                            dest_dir=TMPD)
        except Exception as e:
            wire("export_fail", {"path": f"pre_attack_g{GINDEX}_{turn}.json",
                                 "error": str(e)})
        say(f"--- attack turn {turn} (#{len(ST['attack_turns'])}) ---")
    # token baseline at DeclareAttackers (backfill the attack-turn record
    # if PreCombatMain was missed between polls)
    if phase == "DeclareAttackers" and active == 0 \
            and find_bf_oid(state, 0, DELINA.lower()) is not None:
        if turn not in ST["attack_turns"]:
            ST["attack_turns"].append(turn)
            say(f"--- attack turn {turn} (#{len(ST['attack_turns'])}, "
                f"backfilled) ---")
        if turn not in ST["tokens_before"]:
            ST["tokens_before"][turn] = p0_token_oids(state)
    # token delta once the trigger resolved (stack empty after the target
    # was answered), or at DeclareBlockers/CombatDamage. The reroll prompt
    # (if any) snapshots tokens BEFORE the accept, so the initial delta
    # distinguishes the first roll's branch even when a later accepted
    # reroll creates a token.
    if turn in ST["attack_turns"] and turn not in ST["deltas"] \
            and turn in ST["tokens_before"]:
        answered = any(t == turn for t, _, _ in ST["target_answers"])
        stack_empty = not state.get("stack")
        if (answered and stack_empty and phase == "DeclareAttackers") \
                or phase in ("DeclareBlockers", "CombatDamage"):
            new = p0_token_oids(state) - ST["tokens_before"][turn]
            ST["deltas"][turn] = sorted(new)
            say(f"turn {turn}: token delta = {len(new)} "
                f"(new oids {sorted(new)})")
            wire("token_delta", {"turn": turn, "delta": sorted(new)})
    # branch classification per attack turn
    if turn in ST["attack_turns"]:
        answered = any(t == turn for t, _, _ in ST["target_answers"])
        if answered:
            if turn in ST["tokens_at_reroll"] \
                    and turn in ST["tokens_before"]:
                initial = len(set(ST["tokens_at_reroll"][turn])
                              - ST["tokens_before"][turn])
            elif turn in ST["deltas"]:
                initial = len(ST["deltas"][turn])
            else:
                initial = None
            if initial == 1 and ST["low_turn"] is None:
                ST["low_turn"] = turn
                ST["low_ref"] = [GINDEX, turn]
                say(f"turn {turn}: classified LOW (initial_delta=1)")
                wire("branch_classified", {"turn": turn, "branch": "low",
                                           "gindex": GINDEX})
            if initial == 0 and ST["high_turn"] is None:
                ST["high_turn"] = turn
                ST["high_ref"] = [GINDEX, turn]
                prompted = turn in {t for t, _ in ST["reroll_prompts"]}
                say(f"turn {turn}: classified HIGH (initial_delta=0, "
                    f"prompt={'yes' if prompted else 'no'})")
                wire("branch_classified", {"turn": turn, "branch": "high",
                                           "prompted": prompted,
                                           "gindex": GINDEX})
    # exports on the first low / high turns
    if ST["low_turn"] is not None and not ST["mid_low_exported"] \
            and turn == ST["low_turn"] \
            and phase in ("DeclareBlockers", "CombatDamage"):
        await do_export(p0, "mid_low.json")
        ST["mid_low_exported"] = True
        say(f"=== mid_low exported (turn {turn}, phase={phase}) ===")
    if ST["low_turn"] is not None and not ST["post_low_exported"] \
            and turn == ST["low_turn"] and phase == "PostCombatMain" \
            and active == 0 and not state.get("stack"):
        await do_export(p0, "post_low.json")
        ST["post_low_exported"] = True
        low_oids = set(ST["deltas"].get(turn, []))
        still = low_oids & p0_token_oids(state)
        ST["exile_check"][turn] = len(still) == 0
        say(f"turn {turn}: low-turn token exiled by PostCombatMain: "
            f"{len(still) == 0}")
    if ST["high_turn"] is not None and not ST["mid_high_exported"] \
            and turn == ST["high_turn"] and (
                any(t == turn for t, _ in ST["reroll_prompts"])
                or phase in ("DeclareBlockers", "CombatDamage")):
        await do_export(p0, "mid_high.json")
        ST["mid_high_exported"] = True
        say(f"=== mid_high exported (turn {turn}, phase={phase}) ===")
    if ST["high_turn"] is not None and not ST["post_high_exported"] \
            and turn == ST["high_turn"] and phase == "PostCombatMain" \
            and active == 0 and not state.get("stack"):
        await do_export(p0, "post_high.json")
        ST["post_high_exported"] = True
    # done once we have both branches + closing exports
    if ST["low_turn"] is not None and ST["high_turn"] is not None \
            and ST["post_low_exported"] and ST["post_high_exported"]:
        ST["stage"] = "DONE"
        ST["stop"] = True
        say("=== both branches observed; DONE ===")
    elif len(ST["attack_turns"]) >= GAME_ATTACK_CAP \
            and ST["high_turn"] is None and not ST["stop"]:
        obs["notes"].append(
            f"game {ST['game_code']}: {GAME_ATTACK_CAP} attack turns with no "
            f"high turn (low={ST['low_turn']} "
            f"deltas={ {t: len(v) for t, v in ST['deltas'].items()} }); "
            f"starting a fresh game for a new RNG seed")
        say("=== attack cap reached without high turn; next game ===")
        wire("attack_cap", {"game": ST["game_code"],
                            "attacks": len(ST["attack_turns"])})
        ST["game_result"] = "attack-cap"
        ST["stage"] = "NEXTGAME"
        ST["stop"] = True


# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    data_finding = check_data_level()

    p0 = PhaseClient("P06866")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16866")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    last_progress = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop") \
            and ST.get("stage") != "DONE":
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_progress = time.time()
            else:
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
        await evaluate_flow(p0, obs)
        # game-over detection
        st = st_of(p0)
        if st:
            s = st["state"]
            winner = s.get("winner")
            if s.get("phase") == "GameOver" or winner is not None:
                say(f"game over (winner={winner}); next game")
                wire("game_over", {"winner": winner})
                ST["game_result"] = "game-over"
                ST["stop"] = True
        # stall detection: no new revision for STALL_AFTER seconds
        if not ST.get("stop") and ST.get("stage") == "ATTACK" \
                and time.time() - last_progress > STALL_AFTER:
            say(f"STALL: no new revision for {STALL_AFTER}s at "
                f"rev={p0.revision}; next game")
            wire("stall_detected", {"rev": p0.revision,
                                    "stage": ST.get("stage")})
            obs["notes"].append(
                f"game {ST['game_code']}: wedged (no revision for "
                f"{STALL_AFTER}s at rev {p0.revision}, "
                f"turn {(st_of(p0) or {}).get('state', {}).get('turn_number')}); "
                f"starting a fresh game")
            ST["game_result"] = "stall"
            ST["stop"] = True
        if time.time() - last_diag > 60:
            last_diag = time.time()
            st = st_of(p0)
            if st:
                s = st["state"]
                say(f"DIAG p0: rev={p0.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} stage={ST.get('stage')} "
                    f"prio={my_priority(top_acts(st))} "
                    f"decision={real_decision_pending(st)} "
                    f"cast={ST['cast']['tag'] if ST.get('cast') and ST['cast']['in_flight'] else None} "
                    f"legend={ST['legend_answered']}")

    game_summary = {
        "gindex": GINDEX,
        "game_code": ST.get("game_code"),
        "result": ST.get("game_result")
        or ("done" if ST.get("stage") == "DONE" else "timeout"),
        "attack_turns": list(ST["attack_turns"]),
        "deltas": {str(t): v for t, v in ST["deltas"].items()},
        "target_answers": list(ST["target_answers"]),
    }
    wire("game_summary", game_summary)

    if ST.get("stage") == "DONE" and not any(
            g["gindex"] == GINDEX for g in ST["games"]):
        ST["games"].append(game_summary)

    if ST.get("stage") != "DONE":
        # not done: persist progress and exit; the wrapper starts the
        # next game (fresh RNG seed) unless MAX_GAMES is reached.
        ST["games"].append(game_summary)
        save_progress()
        say(f"game {GINDEX} ended: {game_summary['result']} "
            f"(attacks={len(ST['attack_turns'])})")
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(p0.close(), 15)
        except Exception:
            pass
        try:
            await asyncio.wait_for(p1.close(), 15)
        except Exception:
            pass
        if GINDEX + 1 >= MAX_GAMES:
            say(f"MAX_GAMES ({MAX_GAMES}) reached; finalizing as-is")
        else:
            say(f"wrapper: launch GAME_INDEX={GINDEX + 1} for the next game")
            return {"verdict": "continue", "next_game": GINDEX + 1,
                    "games": ST["games"]}

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    def is_legendary(o):
        return "Legendary" in ((o.get("card_types") or {}).get("supertypes") or [])

    pre = env_state("pre.json")
    A = obs["assert"]
    A["A1_setup_ok"] = ("passed" if pre and setup_gate(pre) else "failed")
    if pre is None:
        obs["notes"].append("pre.json never exported (setup gate unreached)")
    else:
        obs["notes"].append(
            f"pre.json: delina_bf={find_bf_oid(pre, 0, DELINA.lower()) is not None} "
            f"bear_bf={find_bf_oid(pre, 0, BEAR.lower()) is not None} "
            f"life={life_of(pre, 0)}/{life_of(pre, 1)} phase={pre.get('phase')}")
    A["A2_trigger_fires"] = ("passed" if ST["target_answers"] else "failed")
    obs["notes"].append(f"target_answers: {ST['target_answers']}")

    # A3: low branch token properties (run-level: the decisive low turn
    # may come from an earlier game; look up that game's deltas)
    low_ref = ST["low_ref"]
    low_post = env_state("post_low.json")
    if low_ref is not None and low_post is not None:
        gi, low = low_ref
        deltas = {}
        for g in ST["games"]:
            if g["gindex"] == gi:
                deltas = g["deltas"]
                break
        if gi == GINDEX:
            deltas = {str(t): v for t, v in ST["deltas"].items()}
        new_oids = deltas.get(str(low), [])
        attackers = combat_attacker_oids(low_post)
        ok = (len(new_oids) == 1)
        detail = {}
        if ok:
            o = get_obj(low_post, new_oids[0])
            detail = {"name": oname(o), "tapped": bool(o.get("tapped")),
                      "is_token": bool(o.get("is_token")),
                      "in_combat_attackers": str(new_oids[0]) in attackers,
                      "legendary": is_legendary(o),
                      "abilities": json.dumps(o.get("abilities"))[:300]}
            ok = (detail["is_token"] and not detail["legendary"]
                  and "grizzly bears" in str(detail["name"]).lower())
        obs["notes"].append(
            f"A3 low turn {low} (game {gi}): token detail {detail}")
        A["A3_low_branch"] = "passed" if ok else "failed"
        # A7: low-turn token exiled by PostCombatMain? check the token oids
        # against the battlefield tokens in post_low.json
        still = set(new_oids) & p0_token_oids(low_post)
        A["A7_exile_ok"] = "passed" if not still else "failed"
        obs["notes"].append(
            f"A7: low-turn token oids {new_oids} still on BF in "
            f"post_low.json: {sorted(still)}")
    else:
        A["A3_low_branch"] = "not-run" if low_ref is None else "failed"
        A["A7_exile_ok"] = "not-run"
        obs["notes"].append(
            f"A3/A7: low_ref={low_ref} "
            f"post_low={'yes' if low_post else 'no'}")

    A["A4_high_observed"] = ("passed" if ST["high_turn"] is not None else "failed")
    obs["notes"].append(
        f"high_turn={ST['high_turn']} deltas="
        f"{ {t: len(v) for t, v in ST['deltas'].items()} }")

    A["A5_reroll_offered"] = ("passed" if ST["reroll_prompts"] else "failed")
    obs["notes"].append(f"reroll_prompts: {ST['reroll_prompts']}")
    obs["notes"].append(f"reroll_answers: {ST['reroll_answers']}")

    # A6: accepting the reroll produced a further roll resolution (a new
    # token from a 1-14 re-roll, or a repeated prompt from another 15-20)
    if ST["reroll_prompts"]:
        accepted_turns = [t for t, a in ST["reroll_prompts"] if a]
        progressed = False
        for t in accepted_turns:
            before = set(ST["tokens_at_reroll"].get(t, []))
            final_new = set(ST["deltas"].get(t, []))
            if any(o not in before for o in final_new):
                progressed = True  # 1-14 reroll created a token
            same_turn_prompts = [pt for pt, _ in ST["reroll_prompts"]
                                 if pt == t]
            if len(same_turn_prompts) > 1:
                progressed = True  # 15-20 reroll -> prompt repeated
        obs["notes"].append(f"A6 detail: accepted_turns={accepted_turns} "
                            f"progressed={progressed}")
        A["A6_reroll_works"] = "passed" if progressed else "failed"
    else:
        A["A6_reroll_works"] = "not-run"

    obs["notes"].append(f"attack_turns(game {GINDEX})={ST['attack_turns']}")
    obs["notes"].append(f"games={json.dumps(ST['games'], default=str)[:400]}")
    obs["notes"].append(f"legend_answered={ST['legend_answered']}; "
                        f"final stage={ST.get('stage')}")
    obs["notes"].append(f"data-level: {data_finding}")
    obs["notes"].append(
        "protocol-151 driver (v0.113.0, 01130 port): HELLO advertises protocol 151; "
        "CreateGameWithSettings + JoinGameWithPassword + start_when_full; "
        "MulliganDecision via legacy Action; DiscardToHandSize via vi "
        "schema/select; CastSpell via advertised actions; mana is engine "
        "auto-tapped (manual taps are a >90s last-resort fallback only); "
        "Delina trigger target via vi schema/exactChoices candidates "
        "referencing BF creatures of seat 0; reroll via decideOptionalEffect "
        "(first prompt accepted, later declined as control); "
        "DeclareAttackers/Blockers via relations-schema vi opportunity; "
        "playLand in NON_DECISION_CODES; priority-gated passes; 5s re-tick "
        "backstop.")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A["A1_setup_ok"] == "passed" and A["A2_trigger_fires"] == "passed" \
            and A["A3_low_branch"] == "passed" \
            and A["A4_high_observed"] == "passed" \
            and (A["A5_reroll_offered"] == "failed"
                 or (A["A5_reroll_offered"] == "passed"
                     and A["A6_reroll_works"] == "failed")):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 151,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Delina, Wild Mage", 8], ["Grizzly Bears", 12],
                   ["Mountain", 20], ["Forest", 20]],
            "P1": [["Forest", 30], ["Llanowar Elves", 30]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "target_answers": ST["target_answers"],
        "attack_turns": ST["attack_turns"],
        "low_turn": ST["low_turn"],
        "high_turn": ST["high_turn"],
        "low_ref": ST["low_ref"],
        "high_ref": ST["high_ref"],
        "deltas": {str(t): v for t, v in ST["deltas"].items()},
        "games": ST["games"],
        "data_finding": data_finding,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (scenario_6866_01130.py), not direct load.",
            "The die-roll value is not exposed to the client; 15-20 is "
            "identified by its effect signature (trigger fired, 0 new "
            "tokens) while 1-14 provably creates exactly one token.",
        ],
        "setup_line": "P0: 8x Delina + 12x Grizzly Bears + 20x Mountain + "
                      "20x Forest; P1: 30x Forest + 30x Llanowar Elves "
                      "(chump blockers)",
        "contract_line": ("Per attack turn: Delina attacks alone, trigger "
                          "targets the Bear; delta 1 => 1-14 (control), "
                          "delta 0 => 15-20 signature. 15-20 must offer an "
                          "optional reroll; low-turn tokens must exile at "
                          "end of combat."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "target_answers": ST["target_answers"],
                   "reroll_prompts": ST["reroll_prompts"],
                   "reroll_answers": ST["reroll_answers"],
                   "games": ST["games"]}, f,
                  indent=2)
    # copy decisive per-turn pre-attack states into the evidence dir
    for tag, ref in (("low", ST["low_ref"]), ("high", ST["high_ref"])):
        if ref is not None:
            gi, t = ref
            src = f"{TMPD}/pre_attack_g{gi}_{t}.json"
            if os.path.exists(src):
                import shutil
                shutil.copy(src, f"{EVDIR}/pre_attack_{tag}_g{gi}_{t}.json")
                say(f"copied pre_attack_{tag}_g{gi}_{t}.json")
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")
    sys.stdout.flush()

    # Finalize evidence BEFORE closing clients. Each step is guarded so a
    # failure is logged with a traceback instead of silently dropping the
    # rest.
    try:
        render_summary(run, f"{EVDIR}/summary.png")
    except Exception:
        say("render_summary FAILED:")
        say(traceback.format_exc())
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    # manifest AFTER the log closes: write_manifest's own say() line goes to
    # stdout only and every hashed file is final (fixes the chronic
    # scenario_run.log mismatch in earlier runs)
    try:
        import shutil as _shutil
        _shutil.copy(__file__, f"{EVDIR}/scenario_6866_01130.py")
        say("scenario copied into evidence dir")
    except Exception:
        say("scenario copy FAILED:")
        say(traceback.format_exc())
    try:
        write_manifest()
    except Exception:
        say("write_manifest FAILED:")
        say(traceback.format_exc())

    try:
        await asyncio.wait_for(p0.close(), 15)
    except Exception as e:
        print(f"p0.close failed: {e}", flush=True)
    try:
        await asyncio.wait_for(p1.close(), 15)
    except Exception as e:
        print(f"p1.close failed: {e}", flush=True)
    return run


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6866 - Delina, Wild Mage: no reroll on 15-20 "
                    "(revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), f"attack turns: {run.get('attack_turns')}  "
                    f"low={run.get('low_turn', '?')} "
                    f"high={run.get('high_turn', '?')}",
           fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Delina + Bear on BF, life 20/20",
        "A2_trigger_fires": "A2 Delina trigger target prompt answered",
        "A3_low_branch": "A3 low turn: exactly 1 tapped/attacking Bear token",
        "A4_high_observed": "A4 high turn: trigger fired, 0 new tokens",
        "A5_reroll_offered": "A5 'you may roll again' prompt offered",
        "A6_reroll_works": "A6 accepted reroll resolved further",
        "A7_exile_ok": "A7 low-turn token exiled by PostCombatMain",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:9]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6866/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest written ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
