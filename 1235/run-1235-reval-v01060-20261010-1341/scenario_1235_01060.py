#!/usr/bin/env python3
"""Issue #1235: feasible_mana_capacity sum over-counts chain-sacrifice configurations.

Re-validation run on v0.106.0 / protocol 126 (prior: v0.105.0 / protocol 120,
run run-1235-reval-v01050-20261009-1541, verdict reproduced).

Fix revision 01060: identity/pin bump to v0.106.0 only; inherits all 01050
logic unchanged (scenario_1235_01050.py, run run-1235-reval-v01050-20261009-1541).

Protocol-126 driver notes (v0.106.0, 2026-10-10): mechanical port of the
verified v0.105.0 scenario_1235_01050.py (protocol 120; the 2026-10-09 v0.105.0
run reproduced the bug). Only the release pin, build commit,
and ServerHello assertions changed for this v0.106.0 re-validation run.

01021 fixes retained: (1) the bounded-flow acceptance now counts only
UNTAPPED mountains -- the 01020 run's setup left mountains tapped at
X-choice time and the prefix match inflated the acceptance, masking
the +2/+1 over-count deltas; (2) pay-phase completion no longer treats
a pending_cast spell on the stack as paid -- the 01020 run falsely
recorded B3 completed and then stalled 70s+ passing priority against
an unpaid cast; (3) cast_oid is now taken from the actual CastSpell
            action's object_id -- the 01021 run guessed by hand dict order
            and false-failed A3 (recorded oid 48, cast was 18, which the
            authoritative post export showed resolved). (4) pooled mana is
            NOT auto-spent by the 118 engine: KCI/Altar mana must be spent
            via the payment menu's spendPoolMana choices -- the 01021d run's
            game B stalled 60s with {R:2,C:4} pooled and the cast still
            pending; the tick now clicks spendPoolMana while
            expecting=="pay" (tapping a land through the payment menu pays
            1 toward the cost directly: 01021c game B 3 taps -> paid).
            (5) KCI activation is budgeted dynamically -- only while
            tap_pays + pool_spends + untapped_mountains + pool_pips <
            desired (a fixed budget of 2 needlessly sacrificed the
            Phyrexian Altar in 01021d); sacrifice fuel prefers Memnite,
            then KCI itself, then other artifacts. (6) game C's payment
            completion uses the same pending_cast guard as game B.
            Protocol-126 mechanical port of the verified v0.105.0/protocol-120
            scenario_1235_01050.py; interaction conventions unchanged from that
            run -- only the release pin, build commit, RUN_ID and ServerHello
            assertions changed for this v0.106.0 re-validation. Game logic, decks, assertions and
the verdict rule are unchanged; only the interaction surface is adapted:
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full (via
    driver/client.py); deck schema {"name", "main_deck": [<card-name strings>]}.
  - Protocol 118 removed the waiting_for decision surface (now null);
    priority = advertised PassPriority legal action for the viewing seat.
  - MulliganDecision answered as-is via legacy Action (verified accepted on
    118), gated on the MulliganDecision legal action; answered vi
    interactionIds tracked to avoid double-answering.
  - Bottom-after-mulligan via the vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap (the Altar/KCI
    sacrifice-cost prompt uses the identical surface mid-game and must not
    be misfired on).
  - DiscardToHandSize via viewer_interaction only, gated on the
    waitingForKind code with a hand>7 + hand-card-candidates heuristic
    fallback (118 may surface with a generic code for it).
  - PassPriority: legacy action first, then the viewer_interaction
    passPriority action-code fallback.
  - KCI activation: advertised ActivateAbility merged_action first, else
    viewer_interaction exactChoices 'activateAbility' answered with
    {"type":"choose"}. ActivateAbility is submitted WHILE HOLDING PRIORITY;
    submitting after a priority pass leaves the ability unactivated.
  - X-choice: number-schema opportunity in viewer_interaction; submitted as
    {"type":"number","data":{"value":N}}.
  - Priority menus are noisy on 118 (tapLandForMana / untapLandForMana /
    castSpell / activateAbility / passPriority offered at ordinary windows):
    only schema opportunities, decideOptionalEffect, and unknown codes block
    passes; those menus are never treated as target prompts.
  - await asyncio.sleep(0) yield before leg evaluation (driver-race guard);
    5s re-tick backstop for priority-holding clients.
  - never return after an export while holding priority.
  - manifest.sha256 computed AFTER wire_log.jsonl / scenario_run.log close.
  - zero observations map to not-run (never fabricated as failed).
  - ExportAuthoritativeState has NO data field; data.state is a JSON string
    parsed once.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Report (github #1235, status:confirmed, area:engine, mechanic:mana,
priority:p2-wrong-game-result): `feasible_mana_capacity` returns the
single-shot maximum yield per permanent; the `can_feasibly_pay_mana_cost`
(casting.rs) and `max_x_value_excluding` (casting_costs.rs) call sites sum that
value across the whole battlefield, over-counting chain-sacrifice
configurations where one permanent's activation consumes another's existence.

Oracle text (pinned card-data.json):
  Krark-Clan Ironworks: "Sacrifice an artifact: Add {C}{C}."
  Phyrexian Altar: "Sacrifice a creature: Add one mana of any color."
  Fireball: "{X}{R}" -- deals X damage divided among any number of targets.
  Banefire: "{X}{R}" -- deals X damage to any target (single target; used
  instead of Fireball to avoid the DistributeAmong damage-division prompt).
  Endless One: "{X}" -- enters with X +1/+1 counters.

Acceptance criteria from the issue:
  (1) max_x_value for a {X}{R} cost with 2 KCI + 1 Mountain + 1 fodder
      reports X = 2 (not X = 4).
  (2) max_x_value for a {X} cost with 1 KCI + 1 Phyrexian Altar + 1 creature
      reports X = 2 (not X = 3 or X = 4).
Downstream impact claimed: the X chooser shows the over-counted cap and
manual-mode payment then fails after the player has committed.

Three games (native engine, two human-client seats):
  Game A: 2 Krark-Clan Ironworks + Mountains + 1 Memnite (artifact fodder),
          Banefire ({X}{R}) in hand, empty pool, own main-phase priority.
          Submit CastSpell -> read the X-choice number schema's max and
          compare against the bounded-flow acceptance value
          (n_mountains + 2*min(n_kci, n_fodder) - 1 for the {R} shard).
          Then choose X=0, target P1, and complete the cast (control).
  Game B: 1 KCI + 1 Phyrexian Altar + 1 Memnite (shared fuel),
          Endless One ({X}) in hand. Submit CastSpell -> read X max.
          Then commit X=3 (the over-counted cap) and attempt a good-faith
          manual payment: KCI activation(s) + auto PayMana; never cancel.
          Record whether the cast completes or stalls unpayable.
  Game C: same board as B. Commit X=2 (the acceptance cap): KCI activation
          (sac Memnite -> {C}{C}) must pay {2}; Endless One must enter with
          exactly 2 +1/+1 counters (control: the acceptance cap is reachable).

Assertions:
  A1_setup_ok          game A board/hand/phase/pool preconditions reached
  A2_x_cap_overcounted game A X-choice max exceeds the bounded-flow
                       acceptance value (bug signature: each KCI counts the
                       same single Memnite)
  A3_cast_x0_completes game A Banefire X=0 resolves: Banefire in graveyard,
                       P1 life still 20
  B1_setup_ok          game B preconditions reached
  B2_x_cap_overcounted game B X-choice max exceeds the bounded-flow
                       acceptance value n_mountains + 2 (bug signature:
                       KCI and Altar both count the same single Memnite)
  B3_x3_payment        documentary: outcome of the good-faith manual payment
                       for committed X=3 ("completed_via_self_cannibalization"
                       if KCI sacrifices itself for the last {C}{C},
                       "completed", or "stalled_unpayable" for a genuine
                       stall: pending cast, insufficient pool, no mana
                       activation offered, server healthy)
  B3_x3_counters       documentary: Endless One counters after resolution
  C1_setup_ok          game C preconditions reached
  C2_x_cap_observed    game C X-choice schema max (recorded)
  C3_cast_x2_completes game C Endless One X=2 enters with exactly 2 counters

Verdict rule: reproduced iff A2 or B2 shows the X-choice max above the
bounded-flow acceptance value for the observed board (the per-permanent sum
over-counts the shared fuel). not-reproduced iff both maxima equal their
acceptance values. blocked iff a setup never completes.
B3 is documentary either way (it tests the issue's downstream claim against
the real engine, including whether self-cannibalization is permitted).

Evidence: evidence/1235/<run-id>/{pre,post}_{A,B,C}.json, xchoice_{A,B,C}.json,
midpay_B.json, run.json, assertions.json, manifest.sha256, summary.png,
scenario_1235_01060.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-1235-reval-v01060-20261010-1341"
SCENARIO_FILENAME = "scenario_1235_01060.py"
ISSUE = 1235
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.106.0",
    "build_commit": "29e0db3",
    "protocol_version": 126,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.106.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-10: latest stable release v0.106.0 (published "
               "2026-10-10) == pinned release dir; ServerHello "
               "0.106.0/29e0db3/protocol 126 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; reused "
               "live backfill-owned v0.106.0 server on 127.0.0.1:9374 "
               "(pid 3536, owned by runs/backfill-20261010-121157, pinned 2026-10-10)"),
}

for _f, _k in (("server/releases/v0.106.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.106.0/data/card-data.json",
                "card_data_sha256"),
               ("server/releases/v0.106.0/data/draft-pools.json",
                "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(
    open(f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"))

P1_DECK = deck(("forest", 60))

GAMES = [
    {
        "tag": "A",
        "spell": "banefire",
        "cost": "{X}{R}",
        "deck": [("banefire", 12), ("krark-clan ironworks", 12),
                 ("memnite", 12), ("mountain", 24)],
        "land": "mountain",
        "need": {"krark-clan ironworks": 2, "memnite": 1, "mountain": 1},
        "cast_cost": {"krark-clan ironworks": 4},
        "desired_x": 0,
        "mode": "x0_control",
    },
    {
        "tag": "B",
        "spell": "endless one",
        "cost": "{X}",
        "deck": [("endless one", 12), ("krark-clan ironworks", 8),
                 ("phyrexian altar", 4), ("memnite", 12), ("mountain", 24)],
        "land": "mountain",
        "need": {"krark-clan ironworks": 1, "phyrexian altar": 1,
                 "memnite": 1},
        "cast_cost": {"krark-clan ironworks": 4, "phyrexian altar": 3},
        "desired_x": 3,
        "mode": "overcommit",
    },
    {
        "tag": "C",
        "spell": "endless one",
        "cost": "{X}",
        "deck": [("endless one", 12), ("krark-clan ironworks", 8),
                 ("phyrexian altar", 4), ("memnite", 12), ("mountain", 24)],
        "land": "mountain",
        "need": {"krark-clan ironworks": 1, "phyrexian altar": 1,
                 "memnite": 1},
        "cast_cost": {"krark-clan ironworks": 4, "phyrexian altar": 3},
        "desired_x": 2,
        "mode": "x2_control",
    },
]

ASS_KEYS = ["A1_setup_ok", "A2_x_cap_overcounted", "A3_cast_x0_completes",
            "B1_setup_ok", "B2_x_cap_overcounted", "B3_x3_payment",
            "B3_x3_counters",
            "C1_setup_ok", "C2_x_cap_observed", "C3_cast_x2_completes"]

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ---------------------------------------------------------------- helpers

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


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def untapped_lands(state, pid, name):
    return [o for o in bf_by_name(state, pid, name)
            if not get_obj(state, o).get("tapped")]


def gy_names(state, pid):
    return [obj_lname(state, oid) for oid, o in
            (state.get("objects") or {}).items()
            if get_obj(state, oid).get("zone") == "Graveyard"
            and str(get_obj(state, oid).get("controller", -1)) == str(pid)]


def gy_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if get_obj(state, oid).get("zone") == "Graveyard"
            and str(get_obj(state, oid).get("controller", -1)) == str(pid)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def mana_pool(state, pid):
    out = Counter()
    for u in (player_of(state, pid).get("mana_pool") or {}).get("mana", []) or []:
        syms = u.get("symbols") if isinstance(u, dict) else None
        if syms:
            for s in syms:
                out[str(s).upper()] += 1
            continue
        blob = json.dumps(u).lower()
        for color in ("white", "blue", "black", "red", "green", "colorless"):
            if color in blob:
                out[color[0].upper()] += 1
                break
        else:
            out["?"] += 1
    return dict(out)


def pool_total(state, pid):
    return sum(mana_pool(state, pid).values())


def plus_counters(state, oid):
    o = get_obj(state, oid)
    v = o.get("counters")
    if isinstance(v, dict):
        return int(v.get("P1P1", 0))
    if isinstance(v, int):
        return v
    return 0


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_kind_code(st):
    """viewer_interaction waitingForKind code (protocol 118 replaces the
    old waiting_for decision surface; e.g. 'mulligan')."""
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def my_priority(acts):
    """Protocol 118: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def life(state, pid):
    return player_of(state, pid).get("life")


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
        rdata = {"choiceIds": [cid]}
        if stype == "manaGroups":
            rdata["count"] = 1
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": rdata}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def submit_number(c, opp, value, tag):
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "number", "data": {"value": int(value)}}}
    say(f"[{tag}] submitting number X={value}")
    await interact_as(c, sub, tag)
    return sub


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
    assert str(ver).startswith("0.106.0"), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"
    assert str(build) == "29e0db3", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    ok, notes = True, []
    for name, needle in (("krark-clan ironworks", "Sacrifice an artifact"),
                         ("phyrexian altar", "Sacrifice a creature"),
                         ("memnite", None),
                         ("banefire", "deals X damage"),
                         ("endless one", "enters with X +1/+1 counters")):
        e = CARD_DATA.get(name, {})
        if not e:
            ok = False
            notes.append(f"{name}: missing from card data")
        elif needle and needle.lower() not in str(e.get("oracle_text", "")).lower():
            ok = False
            notes.append(f"{name}: oracle shape missing ({needle!r})")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {n: str(CARD_DATA.get(n, {}).get("oracle_text"))[:160]
                              for n in ("krark-clan ironworks",
                                        "phyrexian altar", "memnite",
                                        "banefire", "endless one")}}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    ST["data_level_ok"] = ok
    return ok

# ---------------------------------------------------------------- shared ticks

async def do_mulligan(c, acts, st, pid, tag, keep_names):
    """Protocol 118: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission); the canonical
    surface is the viewer_interaction exactChoices opportunity whose
    interactionId is tracked to avoid double-answering."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if isinstance(s.get("data"), dict)]
            if "mulliganDecision" in codes:
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    if tag.startswith("P0") and len(hn) > 4 and not \
            any(n in hn for n in keep_names) and mull_count < 4:
        say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn,
                          "iid": iid})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 118: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity
    with all chosen candidate ids in one submission.

    CRITICAL: the Altar/KCI sacrifice-cost prompt uses the SAME
    SelectCards + vi-schema surface during gameplay. Disambiguate by the
    waitingForKind code AND game stage: bottoming only happens while the
    code is 'mulligan' at turn 1 / Untap; otherwise leave the prompt for
    handle_expected_interaction.
    """
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
            f"not answering")
        wire(f"{tag}_bottom_no_vi",
             {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    cfg = ST.get("cfgs", {}).get(tag, {})
    key_names = {cfg.get("spell")} if cfg.get("spell") else set()

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in key_names:
            return (1, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (2, str(oid))

    ranked = sorted(cands, key=bkey)
    if any(_cand_reference(ch) is None for ch in ranked):
        def abkey(a):
            oids = a.get("data", {}).get("cards") or []
            oid = oids[0] if oids else None
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm in key_names:
                return (1, str(oid))
            if oid is not None and is_land(get_obj(state, oid)):
                return (0, str(oid))
            return (2, str(oid))
        chosen = sorted(sel_acts, key=abkey)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming via verbatim SelectCards: "
            f"{[obj_lname(state, (a.get('data', {}).get('cards') or [None])[0]) for a in chosen]}")
        for a in chosen:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": a.get("data", {})
                                            .get("cards", [])}})
            await asyncio.sleep(0.4)
        wire("bottom", {"who": tag, "iid": iid, "via": "verbatim_selectcards",
                        "n": n})
        return True
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    sub = {"interactionId": iid,
           "response": {"type": "select",
                        "data": {"choiceIds": [ch.get("id") for ch in picks]}}}
    await interact_as(c, sub, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, skip=False):
    """Protocol 118: DiscardToHandSize surfaces via viewer_interaction;
    gate on the waitingForKind code (the waiting_for surface is gone).

    The 118 vi-discard shape (per the 2026-10-08 #1234 v0.104.0 run, cleanup
    DiscardToHandSize surfaces with vi waitingForKind code 'select' and the
    schema/select-candidates heuristic did NOT match there): if the vi
    heuristic finds nothing, fall back to the verbatim per-card SelectCards
    legal actions (one per hand card), guarded so the fallback
    can never steal the mulligan-bottom or the KCI/Altar sacrifice prompt.
    `skip` must be True while a sacrifice prompt is being answered.
    """
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        # Protocol 118 uses a generic 'choose' waitingForKind code for the
        # cleanup discard prompt. Heuristic: a schema/select opportunity
        # offering our own hand cards while hand > 7 IS the discard prompt
        # (no other prompt in this scenario selects from hand at >7).
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(_cand_reference(ch) in handset for ch in cands):
                found = True
                break
        if not found:
            dkey = (tag, "dcode", code or "none", str(c.revision))
            if dkey not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(dkey)
                say(f"[{tag}] NOTE: hand={len(hand)}>7 but vi kind code={code!r}; "
                    f"vi_ops={len(vi_ops(st))}; trying SelectCards fallback")
                wire(f"{tag}_discard_vi_miss",
                     {"hand": len(hand), "code": code,
                      "vi_ops": vi_ops(st),
                      "act_types": sorted(set(a.get("type") for a in acts))})
            # ---- verbatim SelectCards fallback ----
            if not skip and code != "mulligan":
                sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
                # only fire when every SelectCards oid is one of our hand
                # cards (i.e. this really is the cleanup discard prompt)
                oids_ok = True
                act_by_oid = {}
                for a in sel_acts:
                    cards = (a.get("data") or {}).get("cards") or []
                    if len(cards) != 1 or cards[0] not in handset:
                        oids_ok = False
                        break
                    act_by_oid[cards[0]] = a
                if sel_acts and oids_ok and len(sel_acts) == len(hand):
                    cfg = ST.get("cfgs", {}).get(tag, {})

                    def drank(o):
                        nm = obj_lname(state, o)
                        if nm == cfg.get("land"):
                            return (0, nm)
                        if nm == cfg.get("spell"):
                            return (2, nm)
                        return (1, nm)

                    discards = sorted(hand, key=drank)[:n]
                    fkey = (tag, "vdiscard", str(c.revision))
                    if fkey not in SUBMITTED_OPPS:
                        SUBMITTED_OPPS.add(fkey)
                        say(f"[{tag}] discarding {n} via verbatim "
                            f"SelectCards: {[obj_lname(state, o) for o in discards]}")
                        wire(f"{tag}_discard_verbatim",
                             {"discards": [obj_lname(state, o)
                                           for o in discards]})
                        for o in discards:
                            await submit_as_is(
                                c, {"type": "SelectCards",
                                    "data": {"cards": [o]}})
                            await asyncio.sleep(0.4)
                        return True
            return False
        say(f"[{tag}] discard heuristic: hand={len(hand)}>7, vi kind={code!r} "
            f"offering hand cards -> answering as DiscardToHandSize")
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    cfg = ST.get("cfgs", {}).get(tag, {})

    def rank(o):
        nm = obj_lname(state, o)
        if nm == cfg.get("land"):
            return (0, nm)
        if nm == cfg.get("spell"):
            return (5, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands,
                        key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    say(f"[{tag}] WARNING: DiscardToHandSize without viewer_interaction; not answering")
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    """Answer vi tapLandForMana payment prompts (protocol 118). `needs` is
    a dict like {"R": 1, "generic": 3}; consumed as lands are tapped."""
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
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
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
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def spend_pool_mana(c, g, st, tag, note):
    """Click one available spendPoolMana choice in the payment menu.

    On protocol 118 the engine does NOT auto-consume pooled mana during a
    pending cast: KCI/Altar mana sits in the pool until the player spends it
    via spendPoolMana choices (run 20261004-1235d game B stalled 60s with
    {R:2,C:4} in the pool and the cast still pending). Tapping a land through
    the payment menu pays 1 directly (run 20261004-1235c game B: 3 taps ->
    paid). Returns True after one spend."""
    if g.get("expecting") != "pay":
        return False
    desired = g["cfg"]["desired_x"]
    paid = g.get("tap_pays", 0) + g.get("pool_spends", 0)
    if paid >= desired:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if iid in g.get("answered", set()):
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "spendPoolMana" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                note(f"spend pool mana for payment "
                     f"(paid={paid}/{desired})")
                await answer_vi(c, opp, ch, tag)
                g["answered"].add(iid)
                g["pool_spends"] = g.get("pool_spends", 0) + 1
                wire(f"{tag}_spend_pool",
                     {"choice": ch.get("id"), "pool_spends": g["pool_spends"]})
                await asyncio.sleep(0.6)
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


def cast_spell_offered(state, st, spell):
    """True if a CastSpell action for `spell` is currently offered."""
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and obj_lname(state, a.get("data", {}).get("object_id")) == spell:
            return True, a
    vi = st.get("viewer_interaction") or {}
    if vi.get("canSubmit"):
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "castSpell" in codes and spell in choice_text(ch).lower():
                    return True, {"_vi_choice": ch, "_vi_opp": opp}
    return False, None


async def cast_spell_action(c, g, st, state, note, needs=None):
    cfg = g["cfg"]
    offered, act = cast_spell_offered(state, st, cfg["spell"])
    if not offered:
        return False
    if needs is not None:
        ST["mana_needs"][g["cfg"]["tag"]] = dict(needs)
    # Record the spell's object id from the ACTUAL cast action (data.object_id
    # / card_id). The 01021 run guessed the first hand match by dict order and
    # recorded the wrong Banefire oid (48 vs the cast 18), false-failing A3
    # even though the authoritative post export showed the cast copy resolved.
    # The vi-cast path carries no object_id; fall back to the hand guess there.
    _d = act.get("data") or {}
    _oid = _d.get("object_id") or _d.get("card_id")
    if _oid is not None:
        try:
            g["cast_oid"] = int(_oid)
        except (TypeError, ValueError):
            _oid = None
    if _oid is None:
        for o in hand_ids(state, 0):
            if obj_lname(state, o) == cfg["spell"]:
                g["cast_oid"] = o
                break
    if "_vi_choice" in act:
        opp, ch = act["_vi_opp"], act["_vi_choice"]
        note(f"casting {cfg['spell']} via viewer_interaction choice")
        wire(f"{cfg['tag']}_cast_vi", {"choice": choice_text(ch)[:160]})
        await answer_vi(c, opp, ch, g["cfg"]["tag"])
    else:
        note(f"casting {cfg['spell']}")
        wire(f"{cfg['tag']}_cast",
             {"action": {k: v for k, v in act.items()
                         if not k.startswith("_")}})
        await submit_as_is(c, act)
    await asyncio.sleep(0.8)
    return True


async def try_cast_named(c, g, acts, state, name, needs, note):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][g["cfg"]["tag"]] = dict(needs)
    note(f"P0 casts {name} (oid {oid}) needs={needs}")
    wire(f"{g['cfg']['tag']}_cast_{name}", {"action": a})
    await submit_as_is(c, a)
    await asyncio.sleep(0.5)
    return True


def find_activate(st, source_oid):
    """merged_actions ActivateAbility (source_id match) first; else
    viewer_interaction exactChoices 'activateAbility' answered with
    {"type":"choose"}."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility":
            d = a.get("data") or {}
            if str(d.get("source_id")) == str(source_oid) \
                    or str(a.get("_src_oid")) == str(source_oid):
                return ("submit", a, "ActivateAbility legal_action")
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
                return ("interaction", (opp, ch),
                        "activateAbility via viewer_interaction (choose)")
    return None

# ---------------------------------------------------------------- interactions

def _number_spec(spec):
    """Return (min, max) if this spec is a number schema, else None.
    Robust to a few spec envelope shapes."""
    try:
        blob = json.dumps(spec).lower()
    except Exception:
        return None
    if "number" not in blob:
        return None
    sd = spec.get("data", {}) or {}
    xmin = sd.get("min", spec.get("min"))
    xmax = sd.get("max", spec.get("max"))
    con = (sd.get("constraint", {}) or {}).get("data", {}) or {}
    if xmin is None:
        xmin = con.get("min")
    if xmax is None:
        xmax = con.get("max")
    return xmin, xmax


async def handle_expected_interaction(c, g, st, state, note):
    """Answer xchoice / sacrifice / target prompts only when the game is
    explicitly expecting them (g['expecting']). Returns True if acted.

    Misclassification guard: only `schema`-type opportunities are treated as
    candidate prompts; exactChoices priority menus (passPriority / castSpell
    / tapLandForMana / ...) are never target prompts.
    """
    cfg = g["cfg"]
    tag = cfg["tag"]
    expecting = g.get("expecting")
    if not expecting:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in g["answered"]:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}

        # ---- xchoice (also served under "cast_flow": Banefire asks for
        # targets before X, so the prompt order is not fixed) ----
        if expecting in ("xchoice", "cast_flow") and rtype == "schema":
            num = _number_spec(spec)
            if num is None and expecting == "xchoice":
                continue
            if num is not None:
                xmin, xmax = num
                g["x_max_observed"] = xmax
                bf_now = sorted(Counter(
                    obj_lname(state, o)
                    + ("(tapped)" if get_obj(state, o).get("tapped") else "")
                    for o in bf_oids(state, 0)).items())
                note(f"X-choice offered: min={xmin} max={xmax} "
                     f"(turn {state.get('turn_number')}, bf={bf_now})")
                wire(f"{tag}_xchoice",
                     {"min": xmin, "max": xmax, "interaction": opp,
                      "turn": state.get("turn_number"),
                      "battlefield": bf_now, "pool": mana_pool(state, 0)})
                with open(f"{EVDIR}/xchoice_{tag}.json", "w") as f:
                    json.dump({"min": xmin, "max": xmax,
                               "interaction": opp}, f, indent=1, default=str)
                key = {"A": "A2_x_cap_overcounted",
                       "B": "B2_x_cap_overcounted",
                       "C": "C2_x_cap_observed"}[tag]

                def bf_count(prefix):
                    return sum(c for name, c in bf_now
                               if name.startswith(prefix))

                # Only UNTAPPED mountains count toward the bounded-flow
                # acceptance: a tapped mountain produces no mana, and the
                # 20261004-1235b run showed the setup can leave mountains
                # tapped at X-choice time (inflated acceptance -> false
                # negative on the over-count delta).
                n_mtn = sum(c for name, c in bf_now if name == "mountain")
                n_kci = bf_count("krark-clan ironworks")
                n_memnite = bf_count("memnite")
                if tag == "A":
                    # bounded-flow acceptance: mountains count legitimately;
                    # the shared Memnite fuel bounds total KCI output at 2
                    # per fuel unit; minus the {R} fixed shard.
                    acceptance = n_mtn + 2 * min(n_kci, n_memnite) - 1
                elif tag == "B":
                    # shared single Memnite: KCI takes it (2), Altar gets 0.
                    acceptance = n_mtn + 2
                else:
                    acceptance = None
                g["x_acceptance"] = acceptance
                if acceptance is not None and xmax is not None:
                    over = xmax - acceptance
                    g["ass"][key] = "passed" if over > 0 else "failed"
                    note(f"{key}: max={xmax} vs bounded-flow acceptance "
                         f"{acceptance} (mtn={n_mtn} kci={n_kci} "
                         f"memnite={n_memnite}); delta={over:+d} "
                         f"-> {g['ass'][key]}")
                    wire(f"{tag}_xcap_assert",
                         {"max": xmax, "acceptance": acceptance,
                          "delta": over, "n_mtn": n_mtn, "n_kci": n_kci,
                          "n_memnite": n_memnite,
                          "result": g["ass"][key]})
                else:
                    g["ass"][key] = f"observed_max={xmax}"
                    note(f"{key}: max={xmax}")
                if xmin is None or xmax is None:
                    note(f"X-choice min/max unparseable (min={xmin} "
                         f"max={xmax}); stalling for inspection")
                    wire(f"{tag}_xchoice_unparseable", {"interaction": opp})
                    g["answered"].add(iid)
                    g["expecting"] = None
                    g["x_unavailable"] = True
                    g["phase"] = "done"
                    return True
                desired = cfg["desired_x"]
                if not (xmin <= desired <= xmax):
                    note(f"desired X={desired} outside [{xmin},{xmax}]; "
                         f"cannot proceed with planned commit")
                    g["answered"].add(iid)
                    g["expecting"] = None
                    g["x_unavailable"] = True
                    g["phase"] = "done"
                    return True
                sub = await submit_number(c, opp, desired, tag)
                g["answered"].add(iid)
                if expecting == "cast_flow":
                    g["x_done"] = True
                    if tag == "A":
                        if g.get("target_done"):
                            g["expecting"] = None
                            ST["mana_needs"][tag] = {"R": 1}
                        else:
                            g["expecting"] = "cast_flow"
                    else:
                        g["expecting"] = "pay"
                        g["hold_priority"] = True
                        g["pay_t0"] = time.time()
                        ST["mana_needs"][tag] = {"generic": desired}
                else:
                    g["expecting"] = "pay"
                    g["hold_priority"] = True
                    g["pay_t0"] = time.time()
                    ST["mana_needs"][tag] = {"generic": desired}
                g["expecting_t0"] = time.time()
                note(f"chose X={desired} for {cfg['spell']}")
                wire(f"{tag}_x_sub", {"submission": sub, "x": desired})
                await asyncio.sleep(0.8)
                return True
            # cast_flow with a non-number schema: may be the target prompt;
            # fall through to the target branch below.

        if expecting == "sacrifice" and rtype == "schema":
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            pick = None
            # prefer the configured fuel (Memnite) on our battlefield,
            # then KCI itself (self-cannibalization; legal and keeps the
            # other outlet), then any other own battlefield artifact.
            def _lname(ch):
                ref = _cand_reference(ch)
                if ref is None:
                    return ""
                o = get_obj(state, ref)
                return obj_lname(state, ref) \
                    if o.get("zone") == "Battlefield" \
                    and str(o.get("controller", -1)) == "0" \
                    and is_land(o) is False else ""
            order = {"memnite": 0, "krark-clan ironworks": 1}
            ranked = sorted(
                (ch for ch in avail if _lname(ch)),
                key=lambda ch: (order.get(_lname(ch), 2), _lname(ch)))
            pick = ranked[0] if ranked else None
            if pick is None:
                note(f"sacrifice prompt: no usable candidate; "
                     f"texts={[choice_text(x)[:80] for x in avail]}")
                wire(f"{tag}_sacrifice_no_pick", {"interaction": opp})
                continue
            await answer_vi(c, opp, pick, tag)
            g["answered"].add(iid)
            g["expecting"] = g.pop("_resume_expecting", None)
            g["expecting_t0"] = time.time()
            g["sacs_done"] = g.get("sacs_done", 0) + 1
            ref = _cand_reference(pick)
            pname = obj_lname(state, ref) if ref is not None else "?"
            note(f"sacrificed (#{g['sacs_done']}): {pname}")
            g.setdefault("sac_names", []).append(pname)
            wire(f"{tag}_sacrifice_sub", {"sacrificed": pname})
            await asyncio.sleep(0.8)
            return True

        if expecting in ("target", "cast_flow") and rtype == "schema":
            stype = (spec.get("type") or "")
            if stype not in ("sequence", "select") and _number_spec(spec) is None:
                if iid not in g.get("dumped_iids", set()):
                    g.setdefault("dumped_iids", set()).add(iid)
                    wire(f"{tag}_target_unexpected_spec",
                         {"spec": spec, "interaction": opp})
                    note(f"target prompt has unexpected spec {stype}; "
                         f"leaving for inspection")
                continue
            if _number_spec(spec) is not None:
                continue  # the xchoice branch above handles it
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]

            def cand_seat(ch):
                for s in ch.get("surfaces", []) or []:
                    if s.get("type") not in ("player", "target"):
                        continue
                    d = s.get("data") or {}
                    for k in ("seat", "player", "index"):
                        if d.get(k) is not None:
                            try:
                                return int(d[k])
                            except (TypeError, ValueError):
                                pass
                return None

            pick = next((ch for ch in avail if cand_seat(ch) == 1), None)
            if pick is None:
                note(f"target prompt: no seat-1 candidate; "
                     f"texts={[choice_text(x)[:80] for x in avail]}")
                wire(f"{tag}_target_no_pick", {"interaction": opp})
                continue
            await answer_vi(c, opp, pick, tag)
            g["answered"].add(iid)
            if expecting == "cast_flow":
                g["target_done"] = True
                if g.get("x_done"):
                    g["expecting"] = None
                    ST["mana_needs"][tag] = {"R": 1}
                else:
                    g["expecting"] = "cast_flow"
            else:
                g["expecting"] = None
            note(f"targeted P1 for {cfg['spell']}")
            wire(f"{tag}_target_sub", {"interactionId": iid})
            await asyncio.sleep(0.8)
            return True

        if iid not in g.get("dumped_iids", set()):
            g.setdefault("dumped_iids", set()).add(iid)
            wire(f"{tag}_unmatched_interaction",
                 {"expecting": expecting, "rtype": rtype,
                  "interaction": opp})
            note(f"unmatched interaction while expecting={expecting}: "
                 f"rtype={rtype}")
    return False


# ---------------------------------------------------------------- game logic

def setup_ready(g, state):
    cfg = g["cfg"]
    for name, n in cfg["need"].items():
        if len(bf_by_name(state, 0, name)) < n:
            return False
    if cfg["spell"] not in hand_lnames(state, 0):
        return False
    if pool_total(state, 0) != 0:
        return False
    return my_main(state, 0)


async def setup_script(c, g, acts, state, note):
    """Play lands and setup permanents. Returns True if acted."""
    cfg = g["cfg"]
    tag = cfg["tag"]
    land = cfg["land"]
    if await play_a_land(c, state, 0, acts, f"P0{tag}"):
        return True
    hn = hand_lnames(state, 0)
    if "memnite" in hn and len(bf_by_name(state, 0, "memnite")) < \
            cfg["need"].get("memnite", 0):
        return await try_cast_named(c, g, acts, state, "memnite",
                                    {"generic": 0}, note)
    for prod, cost in cfg["cast_cost"].items():
        if prod in hn and len(bf_by_name(state, 0, prod)) < \
                cfg["need"].get(prod, 0) \
                and len(untapped_lands(state, 0, land)) >= cost:
            return await try_cast_named(c, g, acts, state, prod,
                                        {"generic": cost}, note)
    if setup_ready(g, state):
        g["phase"] = "assert"
        g["assert_t0"] = time.time()
        note("setup complete -> assert phase")
        return False
    return False


async def activate_kci(c, g, st, acts, state, note):
    """Submit ActivateAbility for the first Krark-Clan Ironworks on board.
    MUST be submitted while holding priority (never pass first)."""
    tag = g["cfg"]["tag"]
    kcis = bf_by_name(state, 0, "krark-clan ironworks")
    if not kcis:
        return False
    found = find_activate(st, kcis[0])
    if not found:
        return False
    kind, payload, how = found
    note(f"activating Krark-Clan Ironworks ({how})")
    wire(f"{tag}_activate_kci", {"how": how})
    if kind == "submit":
        await submit_as_is(c, payload)
    else:
        opp, ch = payload
        await answer_vi(c, opp, ch, tag)
        g["answered"].add(opp.get("interactionId"))
    g["_resume_expecting"] = g.get("expecting")
    g["expecting"] = "sacrifice"
    g["expecting_t0"] = time.time()
    await asyncio.sleep(0.5)
    return True


def pay_status(state):
    """Return (endless_one_ids, p0_stack_spells) for the pay phase."""
    ones = bf_by_name(state, 0, "endless one")
    spells = [e for e in (state.get("stack") or [])
              if str(e.get("controller")) == "0"
              and (e.get("kind") or {}).get("type") == "Spell"]
    return ones, spells


async def act_pay_b(c, g, st, acts, state, note):
    """Game B: good-faith manual payment for committed X=3. Never cancel.
    Returns True if acted.

    Payment completion is detected by the spell reaching the stack (payment
    done) or the battlefield (resolved) -- NOT by assuming a stall from a
    quiet viewer, since the engine may conclude the payment step without
    surfacing a PayMana prompt.
    """
    tag = g["cfg"]["tag"]
    pool = mana_pool(state, 0)
    if g.get("expecting") == "sacrifice":
        return False  # prompt is being answered
    if time.time() - g.get("pay_log_t", 0) > 10:
        g["pay_log_t"] = time.time()
        ones, spells = pay_status(state)
        note(f"pay telemetry: pool={pool}, stack_spells={len(spells)}, "
             f"ones_bf={len(ones)}, vikind={vi_kind_code(st)}, "
             f"pending_cast={bool(state.get('pending_cast'))}")
        wire(f"{tag}_pay_telemetry",
             {"pool": pool, "stack_spells": len(spells),
              "ones_bf": len(ones), "vikind": vi_kind_code(st),
              "pending_cast": bool(state.get("pending_cast"))})
    ones, spells = pay_status(state)
    # A spell in pending_cast is NOT paid yet: on protocol 118 the engine
    # puts the spell on the stack in pending-cast state while awaiting mana
    # payment (run 20261004-1235b falsely recorded "completed" here, then
    # stalled passing priority against an unpaid cast). Payment is done
    # only when the cast is no longer pending.
    pending = bool(state.get("pending_cast"))
    if (ones or spells) and not pending:
        g["pay_done"] = True
        g["hold_priority"] = False
        cannibal = "krark-clan ironworks" in (g.get("sac_names") or [])
        mode = ("completed_via_self_cannibalization"
                if cannibal else "completed")
        g["ass"]["B3_x3_payment"] = mode
        note(f"B3: X=3 payment {mode}: pool={pool}, "
             f"stack_spells={len(spells)}, ones_bf={len(ones)}, "
             f"sacs={g.get('sac_names')}")
        wire(f"{tag}_pay_completed",
             {"mode": mode, "pool": pool, "stack_spells": len(spells),
              "ones_bf": len(ones), "sacs": g.get("sac_names"),
              "gy": gy_names(state, 0)})
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/midpay_{tag}.json", "w") as f:
                f.write(post)
            note("mid-pay state exported")
        except Exception as e:
            note(f"mid-pay export failed: {e}")
        g["act_step"] = 2  # let the stack resolve, then record counters
        g["act_wait"] = time.time()
        g["resolve_passes"] = 0
        return False
    if time.time() - g.get("pay_t0", time.time()) > 60:
        kci_acts = [a for a in acts
                    if a["type"] == "ActivateAbility"
                    and "krark-clan ironworks" in
                    obj_lname(state, a.get("data", {}).get("source_id")
                              or a.get("_src_oid"))]
        altar_acts = [a for a in acts
                      if a["type"] == "ActivateAbility"
                      and "phyrexian altar" in
                      obj_lname(state, a.get("data", {}).get("source_id")
                                or a.get("_src_oid"))]
        g["ass"]["B3_x3_payment"] = "stalled_unpayable"
        note(f"B3: X=3 payment STALLED after 60s: pool={pool}, "
             f"gy={gy_names(state, 0)}, "
             f"KCI activations offered={len(kci_acts)}, "
             f"Altar activations offered={len(altar_acts)}, "
             f"vikind={vi_kind_code(st)}")
        wire(f"{tag}_pay_stalled",
             {"pool": pool, "gy": gy_names(state, 0),
              "kci_acts": len(kci_acts), "altar_acts": len(altar_acts),
              "vikind": vi_kind_code(st),
              "pending_cast": bool(state.get("pending_cast"))})
        g["hold_priority"] = False
        await export_post(c, g, note)
        g["phase"] = "done"
        return False
    # good-faith attempt: activate KCI only while the remaining cost cannot
    # be covered by tapping untapped mountains + spending pooled mana.
    # (Run 20261004-1235d: a fixed budget of 2 needlessly sacrificed the
    # Phyrexian Altar, and pooled mana is NOT auto-spent by the engine -- it
    # must be spent via spendPoolMana choices, which the tick now handles
    # before this point.)
    desired = g["cfg"]["desired_x"]
    paid = g.get("tap_pays", 0) + g.get("pool_spends", 0)
    untapped = len(untapped_lands(state, 0, g["cfg"]["land"]))
    pool_pips = sum(mana_pool(state, 0).values())
    if paid + untapped + pool_pips < desired and g.get("sacs_done", 0) < 2:
        if await activate_kci(c, g, st, acts, state, note):
            return True
    return False


async def export_post(c, g, note):
    tag = g["cfg"]["tag"]
    try:
        post = await c.export_state()
        with open(f"{EVDIR}/post_{tag}.json", "w") as f:
            f.write(post)
        g["post_exported"] = True
        note("POST exported")
    except Exception as e:
        note(f"post export failed: {e}")


async def export_tag(c, g, name):
    raw = await c.export_state()
    with open(f"{EVDIR}/{name}_{g['cfg']['tag']}.json", "w") as f:
        f.write(raw)
    say(f"[{g['cfg']['tag']}] exported {name}_{g['cfg']['tag']}.json")
    return json.loads(raw)["state"]


def capture_server_excerpts(game_codes):
    """Grep the live server's log for this run's game codes plus any
    CastSpell 'action applied' lines (the silent-cast-drop diagnosis hook
    from the protocol-118/126 cast-confirmation guard lesson) and write
    server_excerpts.txt into the evidence dir. Called before the manifest
    is computed so the file is covered by manifest.sha256."""
    srv_log = os.path.expanduser(
        "~/workspace/dev/phase-backfill/runs/backfill-20261010-121157/server.log")
    try:
        with open(srv_log, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        with open(f"{EVDIR}/server_excerpts.txt", "w") as f:
            f.write(f"server log unreadable: {e}\n")
        say(f"server excerpts: log unreadable ({e})")
        return
    codes = {str(c) for c in game_codes if c}
    hits, cast_lines = {}, []
    for ln in all_lines:
        for code in codes:
            if code in ln:
                hits.setdefault(code, []).append(ln)
        if 'action_type="CastSpell"' in ln and "action applied" in ln:
            cast_lines.append(ln)
    out = [f"server log: {srv_log} ({len(all_lines)} lines scanned); "
           f"game codes: {sorted(codes)}\n"]
    for code in sorted(hits):
        buf = hits[code][-40:]
        out.append(f"\n===== game code {code} (last {len(buf)} of "
                   f"{len(hits[code])} lines) =====")
        out.extend(buf)
    if cast_lines:
        buf = cast_lines[-30:]
        out.append(f"\n===== CastSpell action-applied (last {len(buf)} of "
                   f"{len(cast_lines)} lines) =====")
        out.extend(buf)
    else:
        out.append("\n===== no CastSpell 'action applied' lines in log =====")
    with open(f"{EVDIR}/server_excerpts.txt", "w") as f:
        f.write("\n".join(l.rstrip("\n") for l in out) + "\n")
    say(f"server excerpts: {sum(len(v) for v in hits.values())} game-code "
        f"lines, {len(cast_lines)} CastSpell-applied lines")

# ---------------------------------------------------------------- act phase

async def finalize_assertion(c, g, state, note):
    cfg = g["cfg"]
    tag = cfg["tag"]
    seen = g["offered_seen"]
    note(f"gate: CastSpell({cfg['spell']}) offered_seen={seen} "
         f"(obs={g['obs']})")
    wire(f"{tag}_gate_assert", {"offered_seen": seen, "obs": g["obs"]})
    g["phase"] = "act"
    g["act_step"] = 0
    g["act_t0"] = time.time()
    note("-> act phase")


async def act_tick(c, g, st, acts, state, note):
    """Per-game act phase. Returns True if acted."""
    cfg = g["cfg"]
    tag = cfg["tag"]
    step = g.get("act_step", 0)
    pool = mana_pool(state, 0)

    if time.time() - g.get("act_t0", time.time()) > 420:
        note("act phase timeout (420s)")
        g["phase"] = "done"
        return False

    mp = my_priority(acts)

    if tag == "A":
        # step 0: submit the cast (target P1 and X=0 in either order).
        # step 1: await Banefire in graveyard; assert P1 still at 20.
        if step == 0 and mp:
            if g.get("expecting"):
                return False
            if await cast_spell_action(c, g, st, state, note):
                g["expecting"] = "cast_flow"
                g["x_done"] = False
                g["target_done"] = False
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            # the CAST Banefire (by object id) must reach the graveyard;
            # name matching alone would false-positive on discarded copies.
            cast_oid = g.get("cast_oid")
            gids = gy_ids(state, 0)
            if cast_oid is not None and int(cast_oid) in gids:
                ok = life(state, 1) == 20
                g["ass"]["A3_cast_x0_completes"] = "passed" if ok else "failed"
                note(f"A3: cast Banefire (oid {cast_oid}) resolved at X=0; "
                     f"P1 life={life(state, 1)} (want 20) -> "
                     f"{g['ass']['A3_cast_x0_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": gy_names(state, 0), "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                await export_post(c, g, note)
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A3_cast_x0_completes"] = "failed"
                note("A3: Banefire never reached graveyard in 120s")
                await export_post(c, g, note)
                g["phase"] = "done"
            return False

    if tag == "B":
        # step 0: submit the cast (xchoice -> X=3 -> manual pay).
        # step 1: good-faith manual payment; detect completion or stall.
        # step 2: payment done -- pass priority, let it resolve, record.
        if step == 0 and mp:
            if g.get("expecting"):
                return False
            if await cast_spell_action(c, g, st, state, note):
                g["expecting"] = "xchoice"
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            return await act_pay_b(c, g, st, acts, state, note)
        if step == 2:
            ones, spells = pay_status(state)
            if ones:
                n = plus_counters(state, ones[0])
                g["ass"]["B3_x3_counters"] = n
                note(f"B3: Endless One resolved with {n} +1/+1 counters "
                     f"(X was 3)")
                wire(f"{tag}_resolved", {"counters": n, "pool": pool})
                await export_post(c, g, note)
                g["phase"] = "done"
                return False
            if g.get("resolve_passes", 0) >= 8 or \
                    time.time() - g.get("act_wait", time.time()) > 60:
                note(f"B3: paid spell not resolved after passes "
                     f"(stack_spells={len(spells)})")
                wire(f"{tag}_resolve_timeout",
                     {"stack_spells": len(spells)})
                await export_post(c, g, note)
                g["phase"] = "done"
                return False
            g["resolve_passes"] = g.get("resolve_passes", 0) + 1
            return False  # fall through to priority passing

    if tag == "C":
        # step 0: submit the cast (xchoice -> X=2 -> manual pay).
        # step 1: KCI activation (sac Memnite -> {C}{C}); detect payment
        #         completion via the spell reaching the stack.
        # step 2: pass priority, let it resolve, assert 2 counters.
        if step == 0 and mp:
            if g.get("expecting"):
                return False
            if await cast_spell_action(c, g, st, state, note):
                g["expecting"] = "xchoice"
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            if g.get("expecting") == "sacrifice":
                return False  # prompt is being answered
            ones, spells = pay_status(state)
            # pending_cast is NOT paid (protocol-118): the spell sits on the
            # stack while awaiting mana. Completion needs it off pending.
            pending = bool(state.get("pending_cast"))
            if (ones or spells) and not pending:
                g["hold_priority"] = False
                g["act_step"] = 2
                g["act_wait"] = time.time()
                g["resolve_passes"] = 0
                note(f"C: payment done (stack_spells={len(spells)}, "
                     f"ones_bf={len(ones)}); letting it resolve")
                wire(f"{tag}_pay_completed",
                     {"stack_spells": len(spells), "ones_bf": len(ones),
                      "pool": pool})
                return False
            if time.time() - g.get("act_wait", time.time()) > 60:
                g["ass"]["C3_cast_x2_completes"] = "failed"
                note("C3: payment never completed in 60s")
                wire(f"{tag}_pay_timeout", {"pool": pool})
                await export_post(c, g, note)
                g["phase"] = "done"
                return False
            if g.get("sacs_done", 0) < 1:
                if await activate_kci(c, g, st, acts, state, note):
                    return True
            return False
        if step == 2:
            ones, spells = pay_status(state)
            if ones:
                n = plus_counters(state, ones[0])
                ok = n == 2
                g["ass"]["C3_cast_x2_completes"] = "passed" if ok else "failed"
                note(f"C3: Endless One on battlefield with {n} +1/+1 "
                     f"counters (want 2) -> {g['ass']['C3_cast_x2_completes']}")
                wire(f"{tag}_resolved",
                     {"counters": n, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                await export_post(c, g, note)
                g["phase"] = "done"
                return False
            if g.get("resolve_passes", 0) >= 8 or \
                    time.time() - g.get("act_wait", time.time()) > 60:
                g["ass"]["C3_cast_x2_completes"] = "failed"
                note(f"C3: paid spell not resolved after passes "
                     f"(stack_spells={len(spells)})")
                wire(f"{tag}_resolve_timeout",
                     {"stack_spells": len(spells)})
                await export_post(c, g, note)
                g["phase"] = "done"
            return False  # fall through to priority passing
    return False


# ---------------------------------------------------------------- ticks

async def p0_tick(c, g):
    cfg = g["cfg"]
    tag = cfg["tag"]
    st = c.latest
    if not st:
        return
    state = st["state"]

    def note(m):
        say(f"[{tag}] {m}")
        g["notes"].append(m)

    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, f"P0{tag}", [cfg["spell"]]):
        return
    if await do_bottom(c, acts, st, 0, f"P0{tag}"):
        return
    # never let the discard fallback steal the KCI/Altar sacrifice prompt
    if g.get("expecting") != "sacrifice":
        if await do_discard_to_handsize(c, acts, st, 0, f"P0{tag}"):
            return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if g.get("expecting") == "pay":
        if await spend_pool_mana(c, g, st, tag, note):
            return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            if g.get("expecting") == "pay":
                # tapping a land through the payment menu pays 1 toward the
                # pending cost directly (run 20261004-1235c game B).
                g["tap_pays"] = g.get("tap_pays", 0) + 1
            return

    if await handle_expected_interaction(c, g, st, state, note):
        return

    # ---- priority gate, then yield before leg evaluation (race fix) ----
    await asyncio.sleep(0)  # yield to the pump before reading fresh state
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if g["phase"] == "setup":
        if my_main(state, 0):
            # setup_script may do nothing; fall through to the priority pass
            # so the game keeps moving.
            if await setup_script(c, g, acts, state, note):
                return
        if my_priority(acts):
            await pass_priority(c, st, acts)
        return

    if g["phase"] == "assert":
        # export PRE at the first ready observation; fall through (never
        # return after an export while holding priority).
        if not g["pre_exported"] and setup_ready(g, state):
            try:
                env = await export_tag(c, g, "pre")
                g["pre_exported"] = True
                g["ass"][f"{tag}1_setup_ok"] = "passed"
                note(f"PRE exported; setup ready "
                     f"(turn {state.get('turn_number')})")
                wire(f"{tag}_pre_meta",
                     {"pool": mana_pool(env, 0),
                      "bf": sorted(Counter(
                          obj_lname(env, o) for o in bf_oids(env, 0)).items())})
            except Exception as e:
                note(f"pre export failed: {e}")
        if g["pre_exported"]:
            offered, act = cast_spell_offered(state, st, cfg["spell"])
            if setup_ready(g, state):
                g["obs"] += 1
                if offered:
                    g["offered_seen"] = True
                    wire(f"{tag}_gate_offer_seen",
                         {"action": {k: v for k, v in act.items()
                                     if not k.startswith("_")}})
            if g["obs"] >= 4 or time.time() - g["assert_t0"] > 60:
                await finalize_assertion(c, g, state, note)
                # NOTE: no return here -- fall through so this same tick
                # still passes priority; returning while holding priority
                # stalls the game.
        if g["phase"] == "assert" and my_priority(acts):
            await pass_priority(c, st, acts)
            return
        if g["phase"] == "act":
            await act_tick(c, g, st, acts, state, note)
            return
        return

    if g["phase"] == "act":
        if await act_tick(c, g, st, acts, state, note):
            return
        # Hold priority while our own interaction is pending or while floated
        # mana awaits the cast: passing could advance the phase/turn and
        # empty the mana pool mid-proof.
        exp = g.get("expecting")
        if exp:
            if time.time() - g.get("expecting_t0", time.time()) > 45:
                note(f"expecting {exp} timed out; clearing")
                g["expecting"] = None
            else:
                return
        if g.get("hold_priority"):
            return
        if my_priority(acts):
            await pass_priority(c, st, acts)
        return

    # phase "done": just pass
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p1_tick(c, g):
    tag = f"P1{g['cfg']['tag']}"
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag, ["forest"]):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)


# ---------------------------------------------------------------- runner

async def run_game(cfg):
    tag = cfg["tag"]
    say(f"===== GAME {tag}: {cfg['spell']} {cfg['cost']} "
        f"(X={cfg['desired_x']}) =====")
    wire("game_start", {"tag": tag, "cfg": cfg})
    g = {"cfg": cfg, "phase": "setup", "notes": [],
         "ass": {}, "answered": set(), "dumped": set(), "dumped_iids": set(),
         "obs": 0, "offered_seen": False,
         "pre_exported": False, "post_exported": False, "mid_exported": False,
         "expecting": None, "sacs_done": 0, "cast_submitted": False,
         "x_max_observed": None, "x_acceptance": None,
         "hold_priority": False, "assert_t0": None, "act_t0": None,
         "act_step": 0, "act_wait": 0}
    t_start = time.time()
    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(*cfg["deck"]))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    g["p0"], g["p1"] = p0, p1
    g["game_code"] = p0.game_code
    ST["cfgs"] = ST.get("cfgs", {})
    ST["cfgs"][f"P0{tag}"] = cfg
    ST["cfgs"][f"P1{tag}"] = {"spell": "forest", "land": "forest"}
    ST["mana_needs"][tag] = {}
    say(f"[{tag}] game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")

    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    try:
        while time.time() - t_start < 560:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.player_id] = time.time()
                else:
                    if time.time() - last_change[c.player_id] > 45:
                        s0 = st["state"]
                        la = [a.get("type") for a in
                              (st.get("legal_actions") or [])][:8]
                        say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                            f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                            f"legal={la} vikind={vi_kind_code(st)}")
                        last_change[c.player_id] = time.time()
                    # Safety net: if this client holds priority but produced
                    # no revision for a while, re-tick anyway -- a tick that
                    # returned without submitting must not stall the game.
                    holds_prio = any(
                        a.get("type") == "PassPriority"
                        for a in (st.get("legal_actions") or []))
                    if not (holds_prio
                            and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        await p0_tick(c, g)
                    else:
                        await p1_tick(c, g)
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                    wire(f"{tag}_tick_error", {"who": c.name,
                                               "err": f"{type(e).__name__}: {e}"})
            if g["phase"] == "done":
                break
        else:
            g["notes"].append("per-game timeout (560s) hit")
            say(f"[{tag}] TIMEOUT")
    finally:
        for key, fname in (("pre", f"pre_{tag}.json"),
                           ("post", f"post_{tag}.json")):
            if not g.get(f"{key}_exported"):
                try:
                    exp = await p0.export_state()
                    with open(f"{EVDIR}/{fname}", "w") as f:
                        f.write(exp)
                    g[f"{key}_exported"] = True
                    say(f"[{tag}] exported {fname} at teardown")
                except Exception as e:
                    g["notes"].append(f"{key} export at teardown failed: {e}")
        try:
            await p0.close()
        except Exception:
            pass
        try:
            await p1.close()
        except Exception:
            pass
    g["duration_s"] = round(time.time() - t_start, 1)
    return g


async def main():
    pidfile = "/tmp/scenario_1235_01060.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_1235_01060 instance is alive "
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


async def _main():
    t_start = time.time()
    ST.update({"cfgs": {}, "mana_needs": {}, "hello_ok": False,
               "data_level_ok": False,
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.gmtime(t_start))})
    await verify_server_hello()
    check_data_level()

    games = {}
    only = os.environ.get("ONLY_GAME")
    cfgs = [c for c in GAMES if not only or c["tag"] == only]
    for cfg in cfgs:
        games[cfg["tag"]] = await run_game(cfg)

    ass = {k: "not-run" for k in ASS_KEYS}
    for tag, g in games.items():
        for k, v in g["ass"].items():
            if k in ass:
                ass[k] = v

    notes_all = []
    for tag, g in games.items():
        notes_all.append(f"--- game {tag} ({g['cfg']['spell']} "
                         f"{g['cfg']['cost']}, X={g['cfg']['desired_x']}) "
                         f"duration {g['duration_s']}s; "
                         f"x_max_observed={g['x_max_observed']}; "
                         f"offered_seen={g['offered_seen']} ---")
        notes_all.extend(g["notes"])

    a2_over = games.get("A", {}).get("ass", {}).get("A2_x_cap_overcounted")
    b2_over = games.get("B", {}).get("ass", {}).get("B2_x_cap_overcounted")
    present = [t for t in "ABC" if t in games]
    setups_ok = bool(present) and all(
        games[t]["ass"].get(f"{t}1_setup_ok") == "passed" for t in present)
    if setups_ok and (a2_over == "passed" or b2_over == "passed"):
        verdict = "reproduced"
    elif setups_ok and a2_over == "failed" and b2_over == "failed":
        verdict = "not-reproduced"
    elif not setups_ok:
        verdict = "blocked"
        notes_all.append("setup incomplete for at least one game; see notes")
    else:
        verdict = "blocked"
        notes_all.append("inconclusive X-max combination "
                         f"(A={a2_over}, B={b2_over}); see notes")

    a2 = games.get("A", {}).get("x_max_observed")
    b2 = games.get("B", {}).get("x_max_observed")
    result_line = (
        f"A1 setup_ok: {ass['A1_setup_ok']}; "
        f"A2 x_cap_overcounted: {ass['A2_x_cap_overcounted']} "
        f"(observed max={a2}, acceptance={games.get('A', {}).get('x_acceptance')}); "
        f"A3 cast_x0_completes: {ass['A3_cast_x0_completes']}; "
        f"B1 setup_ok: {ass['B1_setup_ok']}; "
        f"B2 x_cap_overcounted: {ass['B2_x_cap_overcounted']} "
        f"(observed max={b2}, acceptance={games.get('B', {}).get('x_acceptance')}); "
        f"B3 x3_payment: {ass['B3_x3_payment']}; "
        f"B3 x3_counters: {ass.get('B3_x3_counters', 'n/a')}; "
        f"C1 setup_ok: {ass['C1_setup_ok']}; "
        f"C2 x_cap_observed: {ass['C2_x_cap_observed']}; "
        f"C3 cast_x2_completes: {ass['C3_cast_x2_completes']}"
    )

    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/1235",
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(time.time() - t_start, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": ("runs/backfill-20261010-121157 (live v0.106.0 server on 127.0.0.1:9374, pid 3536, pinned 2026-10-10; evidence under evidence/1235/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 126, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(__file__, "rb").read()).hexdigest(),
        "games": {tag: {
            "spell": g["cfg"]["spell"], "cost": g["cfg"]["cost"],
            "desired_x": g["cfg"]["desired_x"],
            "deck": g["cfg"]["deck"], "game_code": g.get("game_code"),
            "duration_s": g["duration_s"],
            "x_max_observed": g["x_max_observed"],
            "x_acceptance": g.get("x_acceptance"),
            "assertions": g["ass"],
            "offered_seen": g["offered_seen"], "obs": g["obs"],
        } for tag, g in games.items()},
        "decks": {
            "A": "12x Banefire + 12x Krark-Clan Ironworks + 12x Memnite + "
                 "24x Mountain",
            "B": "12x Endless One + 8x Krark-Clan Ironworks + "
                 "4x Phyrexian Altar + 12x Memnite + 24x Mountain",
            "C": "12x Endless One + 8x Krark-Clan Ironworks + "
                 "4x Phyrexian Altar + 12x Memnite + 24x Mountain",
            "P1": "60x Forest (draw-go)",
        },
        "assertions": ass,
        "notes": notes_all,
        "verdict": verdict,
        "result": result_line,
        "scope": "feasible_mana_capacity per-permanent sum over-count in "
                 "chain-sacrifice configurations (Krark-Clan Ironworks / "
                 "Phyrexian Altar); X-choice advertised max vs the issue's "
                 "acceptance caps; manual-payment outcome for an over-counted "
                 "commit. Native engine, two human-client seats.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x spell density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "setup_line": ("A: 2x Krark-Clan Ironworks + Mountain + Memnite, "
                       "Banefire in hand. B/C: KCI + Phyrexian Altar + "
                       "Memnite, Endless One in hand. Empty pool, own main "
                       "phase."),
        "contract_line": ("A: X-choice max over-counted iff bug present "
                          "(acceptance: n_mtn + 2*min(n_kci, n_memnite) - 1); "
                          "X=0 cast completes. B: X-choice max over-counted "
                          "iff bug present (acceptance: n_mtn + 2); commit "
                          "X=3, good-faith manual payment, record completion "
                          "or stall. C: commit X=2, KCI sac Memnite pays {2}, "
                          "Endless One enters with 2 counters."),
        "contract": (
            "Game A ({X}{R} Banefire, 2 KCI + Mountains + Memnite): X chooser "
            "max above the bounded-flow acceptance records the over-count; "
            "then X=0 cast completes as a control. Game B ({X} Endless One, "
            "KCI + Altar + Memnite): X chooser max above acceptance records "
            "the over-count; then X=3 is committed and paid manually in good "
            "faith (KCI activations, auto PayMana, never cancel) to record "
            "completion or an unpayable stall. Game C (same board): X=2 "
            "commits; KCI sac of Memnite pays {2}; Endless One must enter "
            "with exactly 2 +1/+1 counters."
        ),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes_all,
                   "verdict": verdict}, f, indent=1)
    with open(__file__, "rb") as src, \
            open(f"{EVDIR}/" + SCENARIO_FILENAME, "wb") as dst:
        dst.write(src.read())
    capture_server_excerpts([g.get("game_code") for g in games.values()])
    # Close every append-mode evidence file BEFORE hashing the manifest
    # (final verdict lines flush after the hashes would otherwise be
    # computed), then render the PNG, then hash everything.
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    import subprocess
    r = subprocess.run([sys.executable,
                        f"{BACKFILL}/driver/render_summary_1235_01060.py", EVDIR],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout[-500:] if r.stdout else "", flush=True)
    if r.returncode != 0:
        print(f"render failed: {r.stderr[-2000:]}", flush=True)
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read())
                     .hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    v = subprocess.run(["sha256sum", "-c", "manifest.sha256"],
                       capture_output=True, text=True, cwd=EVDIR)
    print(v.stdout[-1500:] if v.stdout else v.stderr[-1500:], flush=True)
    assert v.returncode == 0, "manifest self-check failed"
    print(f"VERDICT: {verdict}", flush=True)
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
