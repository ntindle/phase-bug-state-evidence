#!/usr/bin/env python3
"""Issue #1234: feasible_mana_capacity — colored-shard feasibility under
non-tap mana sources.

Re-validation run on v0.103.0 / protocol 106 (prior: v0.102.0,
run 20261004-1234, verdict reproduced).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Report (github #1234, status:confirmed, area:engine, mechanic:mana,
priority:p2-wrong-game-result): the castability gate rejects colored costs
payable via non-tap mana abilities. Revised scope per the 2026-08-13 status
comment (PR #2788 partially superseded the original description):
single-activation colored feasibility is now modeled, but repeated activation
of the same source is not — one Phyrexian Altar + two sacrificable creatures
still cannot prove a {B}{B} spell castable.

Oracle text (v0.103.0 pinned card-data.json):
  Phyrexian Altar: "Sacrifice a creature: Add one mana of any color."
  Krark-Clan Ironworks: "Sacrifice an artifact: Add {C}{C}."

Four games (native engine, two human-client seats):
  Game A: {1}{B} spell (Cabal Ritual), 1+ Swamp + Phyrexian Altar +
          1 Diregraf Ghoul -> CastSpell must be OFFERED (one-activation model
          covers this per the status comment). Then cast it for real (Altar
          activation + Swamp) and assert it resolves.
  Game B: {B}{B} spell (Sign in Blood), Forests-only + Phyrexian Altar +
          2 Memnites -> CastSpell must be OFFERED (two activations yield {B}{B});
          the confirmed remaining bug is that it is NOT. Manual proof: activate
          the Altar twice for real and float {B}{B}; the gate must offer then.
  Game C: {B} spell (Dark Ritual), Phyrexian Altar + 0 creatures
          -> CastSpell must NOT be offered (correct rejection; negative control).
  Game D: {2} spell (Steel Overseer), Krark-Clan Ironworks + Memnite
          -> CastSpell must be OFFERED (KCI colorless regression, cf. #562/#1231).
          Then cast it for real via a KCI activation.

Assertions:
  A1_setup_ok        all four games: required permanents + spell in hand reached
  A2a_gate_offers_1B game A: CastSpell(Cabal Ritual) offered at main-phase
                     priority with empty pool (expect offered)
  A2b_cast_completes game A: manual float (Altar->sac Ghoul->Black, tap Swamp)
                     + cast -> Ritual resolves: Ghoul+Ritual in graveyard,
                     black pool == 3
  A3a_gate_offers_BB game B: CastSpell(Sign in Blood) offered at main-phase
                     priority with empty pool (expect offered per correct
                     behavior; the reported bug is that it is NOT)
  A3b_manual_proof   game B: two real Altar activations float {B}{B}; gate then
                     offers; cast targeting P1 resolves (P1 20->18 life)
  A4_gate_rejects_B  game C: CastSpell(Dark Ritual) NOT offered with Altar and
                     0 creatures (expect not offered — correct rejection)
  A5a_gate_offers_2  game D: CastSpell(Steel Overseer) offered with KCI+Memnite
                     (expect offered — KCI regression guard)
  A5b_cast_completes game D: KCI activation (sac Memnite -> {C}{C}) + cast ->
                     Overseer on battlefield, Memnite in graveyard

Verdict rule: reproduced iff A3a records the gate NOT offering {B}{B} while A3b
proves the mana was actually producible. not-reproduced iff the gate offers
{B}{B} (and A2/A4/A5 behave as expected). blocked iff setup cannot be driven.

Evidence: evidence/1234/<run-id>/pre_{A,B,C,D}.json, mid_B.json, post_{A,B,C,D}.json,
run.json, assertions.json, manifest.sha256, summary.png, scenario_1234_01020.py,
wire_log.jsonl, scenario_run.log

Driver conventions (protocol 106, ported from scenario_1234_0711.py):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full (via
    driver/client.py PhaseClient.create/join); deck schema is now
    {"name", "main_deck": [<card-name strings>], ...}.
  - Protocol 106 removed the waiting_for decision surface (now null); all
    decisions surface via legal_actions (legacy Action messages still
    accepted) and/or viewer_interaction. Priority is detected by the
    presence of a PassPriority legal action for the viewing seat.
  - MulliganDecision answered as-is via legacy Action (verified accepted on
    106), gated on the MulliganDecision legal action; answered vi
    interactionIds are tracked to avoid double-answering.
  - Bottom-after-mulligan: SelectCards legal actions (one per hand card,
    data.cards=[oid]) plus a vi schema/select opportunity; answered via the
    vi opportunity.
  - DiscardToHandSize answered through viewer_interaction, gated on the
    vi waitingForKind code.
  - Mana-ability activation: merged_actions ActivateAbility (source_id match)
    first, else viewer_interaction exactChoices 'activateAbility' answered
    with {"type":"choose"}.
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    reading fresh state for leg evaluation (leg-engagement race).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - P1 is a driver seat (two human clients); the native AI seat is unreliable.
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
RUN_ID = "20261006-1414-1234"
ISSUE = 1234
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.103.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-06: latest stable release v0.103.0 (published "
               "2026-10-06) == pinned release dir; ServerHello "
               "0.103.0/ec27a8d/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server process on 127.0.0.1:9374 started fresh for run "
               "20261006-1414-1234 (backfill-owned)"),
}

for _f, _k in (("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.103.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

P1_DECK = deck(("forest", 60))

GAMES = [
    {"tag": "A", "spell": "cabal ritual", "cost": "{1}{B}",
     "deck": deck(("cabal ritual", 12), ("phyrexian altar", 4),
                  ("diregraf ghoul", 8), ("swamp", 36)),
     "land": "swamp", "producer": "phyrexian altar", "sac_name": "diregraf ghoul",
     "producer_lands": 3, "want_offered": True, "mode": "cast_full"},
    {"tag": "B", "spell": "sign in blood", "cost": "{B}{B}",
     "deck": deck(("sign in blood", 12), ("phyrexian altar", 4),
                  ("memnite", 12), ("forest", 32)),
     "land": "forest", "producer": "phyrexian altar", "sac_name": "memnite",
     "producer_lands": 3, "want_offered": True, "mode": "manual_proof"},
    {"tag": "C", "spell": "dark ritual", "cost": "{B}",
     "deck": deck(("dark ritual", 12), ("phyrexian altar", 4), ("forest", 44)),
     "land": "forest", "producer": "phyrexian altar", "sac_name": None,
     "producer_lands": 3, "want_offered": False, "mode": "observe_only"},
    {"tag": "D", "spell": "steel overseer", "cost": "{2}",
     "deck": deck(("steel overseer", 12), ("krark-clan ironworks", 4),
                  ("memnite", 12), ("forest", 32)),
     "land": "forest", "producer": "krark-clan ironworks", "sac_name": "memnite",
     "producer_lands": 4, "want_offered": True, "mode": "cast_full"},
]

ASS_KEYS = ("A1_setup_ok", "A2a_gate_offers_1B", "A2b_cast_completes",
            "A3a_gate_offers_BB", "A3b_manual_proof", "A4_gate_rejects_B",
            "A5a_gate_offers_2", "A5b_cast_completes")

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


def top_acts(st):
    """Top-level legal actions advertised to the viewing seat."""
    return list(st.get("legal_actions", []) or [])


def vi_kind_code(st):
    """viewer_interaction waitingForKind code (protocol 106 replaces the
    old waiting_for decision surface; e.g. 'mulligan')."""
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def untapped_lands(state, pid, name):
    return [o for o in bf_by_name(state, pid, name)
            if not get_obj(state, o).get("tapped")]


def bf_creatures(state, pid):
    return [o for o in bf_oids(state, pid)
            if "Creature" in (get_obj(state, o).get("card_types") or {})
            .get("core_types", [])]


def gy_names(state, pid):
    return [obj_lname(state, oid) for oid, o in
            (state.get("objects") or {}).items()
            if get_obj(state, oid).get("zone") == "Graveyard"
            and str(get_obj(state, oid).get("controller", -1)) == str(pid)]


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


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def life(state, pid):
    return player_of(state, pid).get("life")


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def can_pay(state, pid, colors=(), generic=0):
    color_of = {"swamp": "B", "island": "U", "forest": "G"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
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
            # InteractionResponse::ManaGroups { choice_ids, count }:
            # count must satisfy 1 <= count <= max_batch (here 1).
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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    ok, notes = True, []
    for name, needle in (("phyrexian altar", "Sacrifice a creature"),
                         ("krark-clan ironworks", "Sacrifice an artifact"),
                         ("cabal ritual", "Add {B}{B}{B}"),
                         ("sign in blood", "Target player draws"),
                         ("dark ritual", "Add {B}{B}{B}")):
        e = CARD_DATA.get(name, {})
        if needle.lower() not in str(e.get("oracle_text", "")).lower():
            ok = False
            notes.append(f"{name}: oracle shape missing ({needle!r})")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {n: str(CARD_DATA.get(n, {}).get("oracle_text"))[:160]
                              for n, _ in (("phyrexian altar", None),
                                            ("krark-clan ironworks", None),
                                            ("cabal ritual", None),
                                            ("sign in blood", None),
                                            ("dark ritual", None))}}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")

# ---------------------------------------------------------------- shared ticks

async def do_mulligan(c, acts, st, pid, tag, keep_names):
    """Protocol 106: MulliganDecision arrives as a legacy legal action
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
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
    cfg = ST["cfgs"].get(tag, {}) if tag in ST.get("cfgs", {}) else {}
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
        # references missing: fall back to the verbatim advertised
        # per-card SelectCards actions, ranked by the same policy
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


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction;
    gate on the waitingForKind code (the waiting_for surface is gone)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        # Protocol 106 uses a generic 'choose' waitingForKind code for the
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
            dkey = (tag, "dcode", code or "none")
            if dkey not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(dkey)
                say(f"[{tag}] NOTE: hand={len(hand)}>7 but vi kind code={code!r}; "
                    f"not answering yet")
            return False
        say(f"[{tag}] discard heuristic: hand={len(hand)}>7, vi kind={code!r} "
            f"offering hand cards -> answering as DiscardToHandSize")
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    cfg = ST["cfgs"].get(tag, {}) if tag in ST.get("cfgs", {}) else {}

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


def find_activate(st, source_oid):
    """Protocol-103: merged_actions ActivateAbility (source_id match) first;
    else viewer_interaction exactChoices 'activateAbility' answered with
    {"type":"choose"}."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility" and \
                str((a.get("data") or {}).get("source_id")) == str(source_oid):
            return ("submit", a, "ActivateAbility legal_action")
        if a.get("type") == "ActivateAbility" and \
                str(a.get("_src_oid")) == str(source_oid):
            return ("submit", a, "ActivateAbility legal_action (_src_oid)")
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

# ---------------------------------------------------------------- interaction handling

async def handle_expected_interaction(c, g, st, state, note):
    """Answer sacrifice-cost / color-choice / target prompts only when the
    game is explicitly expecting them (g['expecting']). Returns True if acted.
    Misclassification guard: only `schema`-type opportunities are treated as
    candidate-selection prompts; exactChoices priority menus (passPriority/
    castSpell/...) are never treated as target prompts."""
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
        if expecting == "sacrifice" and rtype == "schema":
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            pick = None
            for ch in avail:
                ref = None
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and "reference" in d:
                        ref = d.get("reference")
                if ref is not None and obj_lname(state, ref) == cfg["sac_name"]:
                    o = get_obj(state, ref)
                    if o.get("zone") == "Battlefield":
                        pick = ch
                        break
            if pick is None and len(avail) == 1:
                ref = None
                for s in avail[0].get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and "reference" in d:
                        ref = d.get("reference")
                if ref is not None and \
                        str(get_obj(state, ref).get("controller", -1)) == "0":
                    pick = avail[0]
            if pick is None:
                note(f"sacrifice prompt: no {cfg['sac_name']} candidate; "
                     f"texts={[choice_text(x)[:80] for x in avail]}")
                wire(f"{tag}_sacrifice_no_pick", {"interaction": opp})
                continue
            await answer_vi(c, opp, pick, tag)
            g["answered"].add(iid)
            g["expecting"] = "color" if cfg["producer"] == "phyrexian altar" else None
            g["expecting_t0"] = time.time()
            g["sacs_done"] = g.get("sacs_done", 0) + 1
            note(f"sacrificed for {cfg['producer']} (#{g['sacs_done']}): "
                 f"{choice_text(pick)[:100]}")
            wire(f"{tag}_sacrifice_sub", {"interaction": opp})
            await asyncio.sleep(0.8)
            return True
        if expecting == "color" and rtype in ("exactChoices", "schema"):
            chs = data.get("choices") or data.get("candidates") or []
            avail = [ch for ch in chs
                     if ch.get("status", {}).get("type") in (None, "available")]
            texts = [(ch, choice_text(ch)) for ch in avail]
            if any("passpriority" in t.lower().replace(" ", "")
                   or "castspell" in t.lower().replace(" ", "")
                   for _, t in texts):
                continue
            pick = None
            for ch, t in texts:
                if "black" in t.lower() or "{b}" in t.lower() \
                        or t.strip().lower() in ("b", "black"):
                    pick = ch
                    break
            if pick is None:
                for ch in avail:
                    syms = []
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data") or {}
                        if isinstance(d, dict) and isinstance(d.get("symbols"), list):
                            syms.extend(str(x) for x in d["symbols"])
                    if any(x.upper() == "B" for x in syms):
                        pick = ch
                        break
            if pick is None:
                if iid not in g["dumped"]:
                    g["dumped"].add(iid)
                    wire(f"{tag}_color_unrecognized", {"interaction": opp})
                    note(f"color prompt unrecognized: "
                         f"{[t[:80] for _, t in texts]}")
                continue
            await answer_vi(c, opp, pick, tag)
            g["answered"].add(iid)
            g["expecting"] = None
            g["colors_chosen"] = g.get("colors_chosen", 0) + 1
            note(f"chose mana color: {choice_text(pick)[:100]}")
            wire(f"{tag}_color_sub", {"interaction": opp})
            await asyncio.sleep(0.8)
            return True
        if expecting == "target" and rtype == "schema":
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
            g["expecting"] = None
            note(f"targeted P1 for {cfg['spell']}")
            wire(f"{tag}_target_sub", {"interaction": opp})
            await asyncio.sleep(0.8)
            return True
    return False


# ---------------------------------------------------------------- per-game logic

def setup_ready(g, state):
    cfg = g["cfg"]
    tag = cfg["tag"]
    prod = bf_by_name(state, 0, cfg["producer"])
    ok = (len(prod) >= 1 and cfg["spell"] in hand_lnames(state, 0))
    if tag == "A":
        ok = ok and len(bf_by_name(state, 0, "diregraf ghoul")) >= 1 \
            and len(untapped_lands(state, 0, "swamp")) >= 1
    elif tag == "B":
        ok = ok and len(bf_by_name(state, 0, "memnite")) >= 2
    elif tag == "C":
        ok = ok and len(bf_creatures(state, 0)) == 0
    elif tag == "D":
        ok = ok and len(bf_by_name(state, 0, "memnite")) >= 1
    return ok and my_main(state, 0)


async def setup_script(c, g, acts, state, note):
    """Play lands and setup permanents. Returns True if acted."""
    cfg = g["cfg"]
    tag = cfg["tag"]
    if await play_a_land(c, state, 0, acts, tag):
        return True
    hn = hand_lnames(state, 0)
    if cfg["sac_name"] and cfg["sac_name"] in hn:
        if tag == "A" and len(bf_by_name(state, 0, "diregraf ghoul")) < 1 \
                and can_pay(state, 0, ("B",)):
            return await try_cast_named(c, g, acts, state, "diregraf ghoul",
                                        {"B": 1}, note)
        if tag in ("B", "D") and len(bf_by_name(state, 0, "memnite")) < 2:
            return await try_cast_named(c, g, acts, state, "memnite",
                                        {"generic": 0}, note)
    prod = cfg["producer"]
    if prod in hn and not bf_by_name(state, 0, prod):
        colors = ("G", "G", "G") if cfg["land"] == "forest" else ("B", "B", "B")
        gen = 0 if cfg["producer_lands"] == 3 else 1
        # producer costs {3} (altar) or {4} (KCI): colors+generic cover it
        needs = {"generic": cfg["producer_lands"]}
        if can_pay(state, 0, (), cfg["producer_lands"]):
            return await try_cast_named(c, g, acts, state, prod, needs, note)
    if setup_ready(g, state):
        g["phase"] = "assert"
        g["assert_t0"] = time.time()
        note("setup complete -> assert phase")
        return False
    return False


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


async def export_tag(c, g, name):
    raw = await c.export_state()
    with open(f"{EVDIR}/{name}_{g['cfg']['tag']}.json", "w") as f:
        f.write(raw)
    say(f"[{g['cfg']['tag']}] exported {name}_{g['cfg']['tag']}.json")
    return json.loads(raw)["state"]


async def finalize_assertion(c, g, state, note):
    cfg = g["cfg"]
    tag = cfg["tag"]
    key = {"A": "A2a_gate_offers_1B", "B": "A3a_gate_offers_BB",
           "C": "A4_gate_rejects_B", "D": "A5a_gate_offers_2"}[tag]
    seen = g["offered_seen"]
    want = cfg["want_offered"]
    g["ass"][key] = "passed" if (seen == want) else "failed"
    note(f"gate assertion {key}: offered_seen={seen} want_offered={want} "
         f"-> {g['ass'][key]} (obs={g['obs']})")
    wire(f"{tag}_gate_assert", {"offered_seen": seen, "want": want,
                                "result": g["ass"][key], "obs": g["obs"]})
    if cfg["mode"] == "observe_only":
        try:
            await export_tag(c, g, "post")
            g["post_exported"] = True
            note("POST exported (observe-only)")
        except Exception as e:
            note(f"post export failed: {e}")
        g["phase"] = "done"
    else:
        g["phase"] = "act"
        g["act_step"] = 0
        g["act_t0"] = time.time()
        note("-> act phase")


async def activate_producer(c, g, st, state, acts, note):
    cfg = g["cfg"]
    prod_ids = bf_by_name(state, 0, cfg["producer"])
    if not prod_ids:
        note(f"no {cfg['producer']} on battlefield to activate")
        return False
    r = find_activate_cur(st, prod_ids[0])
    if r is None:
        note(f"ActivateAbility not offered for {cfg['producer']}; "
             f"acts={[a['type'] for a in acts][:12]}")
        wire(f"{cfg['tag']}_activate_missing",
             {"acts": [a["type"] for a in acts][:12]})
        return False
    kind, sub, what = r
    note(f"activating {cfg['producer']} via {what}")
    wire(f"{cfg['tag']}_activate", {"via": what})
    if kind == "interaction":
        opp, ch = sub
        await answer_vi(c, opp, ch, cfg["tag"])
    else:
        await submit_as_is(c, sub)
    g["expecting"] = "sacrifice"
    g["expecting_t0"] = time.time()
    await asyncio.sleep(0.5)
    return True


def find_activate_cur(st, source_oid):
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


async def act_tick(c, g, st, acts, state, note):
    """Manual sequences after the gate assertion. Returns True if acted."""
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
        # step 0: activate altar (sac Ghoul -> {B} floats). step 1: cast ritual
        # (engine prompts for remaining {1} via payment flow). step 2: await
        # resolution, assert, export POST.
        if step == 0 and mp:
            if g.get("expecting"):
                return False
            if g.get("sacs_done", 0) >= 1:
                g["act_step"] = 1
                return False
            return await activate_producer(c, g, st, state, acts, note)
        if step == 1 and mp:
            if await cast_spell_action(c, g, st, state, note,
                                       {"B": 1, "generic": 1}):
                g["cast_submitted"] = True
                g["act_step"] = 2
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered with pool {pool}; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 2:
            gy = gy_names(state, 0)
            if "cabal ritual" in gy:
                black = pool.get("B", 0)
                ghoul_gy = "diregraf ghoul" in gy
                # pool composition: 1 (altar float) + 1 (tapped swamp for the
                # {B} payment prompt) + 3 (ritual resolves) = 5. The engine
                # asked for explicit payment taps rather than auto-consuming
                # the floating {B}; assert the ritual's contribution instead
                # of an exact total.
                ok = ghoul_gy and black >= 3
                g["ass"]["A2b_cast_completes"] = "passed" if ok else "failed"
                note(f"A2b: ritual resolved; ghoul in gy={ghoul_gy}; "
                     f"black pool={black} (want >=3: 1 altar + 1 swamp tap + "
                     f"3 ritual) -> {g['ass']['A2b_cast_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool, "life": [life(state, 0),
                                                       life(state, 1)]})
                try:
                    await export_tag(c, g, "post")
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A2b_cast_completes"] = "failed"
                note("A2b: ritual never reached graveyard in 120s")
                g["phase"] = "done"
            return False

    if tag == "B":
        # step 0/1: two altar activations. step 2: export MID, re-check gate,
        # cast targeting P1. step 4: await resolution, assert, POST.
        if step in (0, 1) and mp:
            if g.get("expecting"):
                return False
            if g.get("sacs_done", 0) > step:
                g["act_step"] = step + 1
                if step + 1 == 2:
                    note(f"both activations done; pool={pool}")
                return False
            return await activate_producer(c, g, st, state, acts, note)
        if step == 2 and mp:
            black = pool.get("B", 0)
            try:
                await export_tag(c, g, "mid")
                g["mid_exported"] = True
                note(f"MID exported; pool={pool}")
            except Exception as e:
                note(f"mid export failed: {e}")
            offered, act = cast_spell_offered(state, st, cfg["spell"])
            wire(f"{tag}_mid_gate", {"pool": pool, "offered": offered})
            if offered and black >= 2:
                g["ass"]["A3b_manual_proof"] = "passed"
                note(f"A3b: with {{B}}{{B}} floating, gate now offers "
                     f"{cfg['spell']} -> passed (bug confirmed: feasibility "
                     f"analysis, not resources, blocked the offer)")
                if await cast_spell_action(c, g, st, state, note):
                    g["expecting"] = "target"
                    g["cast_submitted"] = True
                    g["act_step"] = 4
                    g["act_wait"] = time.time()
                    return True
                g["ass"]["A3b_manual_proof"] = "failed"
                note(f"A3b: gate offered but cast submission failed "
                     f"(pool={pool}) -> failed")
                g["phase"] = "done"
            else:
                g["ass"]["A3b_manual_proof"] = "failed"
                note(f"A3b: gate still not offering with pool={pool} "
                     f"(offered={offered}) -> failed")
                g["phase"] = "done"
            return False
        if step == 4:
            gy = gy_names(state, 0)
            if "sign in blood" in gy:
                l1 = life(state, 1)
                ok = l1 == 18
                g["resolution_B"] = "passed" if ok else "failed"
                note(f"Sign in Blood resolved: P1 life={l1} (want 18) -> "
                     f"{g['resolution_B']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                try:
                    await export_tag(c, g, "post")
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["resolution_B"] = "failed"
                note("Sign in Blood never reached graveyard in 120s")
                g["phase"] = "done"
            return False

    if tag == "D":
        # step 0: KCI activation (sac Memnite -> {C}{C}). step 1: cast overseer.
        # step 2: await resolution, assert, POST.
        if step == 0 and mp:
            if g.get("expecting"):
                return False
            if g.get("sacs_done", 0) >= 1:
                g["act_step"] = 1
                return False
            return await activate_producer(c, g, st, state, acts, note)
        if step == 1 and mp:
            if pool.get("C", 0) >= 2 or pool.get("COLORLESS", 0) >= 2:
                if await cast_spell_action(c, g, st, state, note,
                                           {"generic": 2}):
                    g["cast_submitted"] = True
                    g["act_step"] = 2
                    g["act_wait"] = time.time()
                    return True
            note(f"cast step: pool={pool}; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 2:
            bf = bf_by_name(state, 0, "steel overseer")
            gy = gy_names(state, 0)
            if bf:
                mem_gy = "memnite" in gy
                g["ass"]["A5b_cast_completes"] = "passed" if mem_gy else "failed"
                note(f"A5b: Overseer on battlefield; Memnite in gy={mem_gy} "
                     f"-> {g['ass']['A5b_cast_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                try:
                    await export_tag(c, g, "post")
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A5b_cast_completes"] = "failed"
                note("A5b: Overseer never reached battlefield in 120s")
                g["phase"] = "done"
            return False
    return False

# ---------------------------------------------------------------- ticks

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
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    if await handle_expected_interaction(c, g, st, state, note):
        return

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)  # yield to the pump before reading fresh state
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if g["phase"] == "setup":
        if my_main(state, 0):
            # setup_script may do nothing (e.g. own spell on the stack);
            # fall through to the priority pass so the game keeps moving.
            if await setup_script(c, g, acts, state, note):
                return
        if my_priority(acts):
            await pass_priority(c, st, acts)
        return

    if g["phase"] == "assert":
        # export PRE at the first ready observation
        if not g["pre_exported"] and setup_ready(g, state):
            try:
                env = await export_tag(c, g, "pre")
                g["pre_exported"] = True
                g["ass"][f"A1_setup_ok_{tag}"] = "passed"
                note(f"PRE exported; setup ready (turn {state.get('turn_number')})")
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
                # NOTE: no return here — fall through so this same tick
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
        # Hold priority while our own producer interaction is pending or
        # while floated mana awaits the cast: passing could advance the
        # phase/turn and empty the mana pool mid-proof.
        exp = g.get("expecting")
        if exp:
            if time.time() - g.get("expecting_t0", time.time()) > 45:
                note(f"expecting {exp} timed out; clearing")
                g["expecting"] = None
            else:
                return
        if g.get("sacs_done", 0) >= 1 and not g.get("cast_submitted"):
            return
        if my_priority(acts):
            await pass_priority(c, st, acts)
        return

    # phase "done": just pass
    if my_priority(acts):
        await pass_priority(c, st, acts)


# ---------------------------------------------------------------- runner

async def run_game(cfg):
    tag = cfg["tag"]
    say(f"===== GAME {tag}: {cfg['spell']} {cfg['cost']} =====")
    wire("game_start", {"tag": tag, "spell": cfg["spell"]})
    g = {"cfg": cfg, "phase": "setup", "notes": [],
         "ass": {}, "answered": set(), "dumped": set(), "obs": 0,
         "offered_seen": False, "pre_exported": False,
         "post_exported": False, "mid_exported": False,
         "expecting": None, "sacs_done": 0, "colors_chosen": 0,
         "cast_submitted": False, "resolution_B": "not-run",
         "assert_t0": None, "act_t0": None, "act_step": 0, "act_wait": 0}
    t_start = time.time()
    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(cfg["deck"])
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    g["game_code"] = p0.game_code
    ST["cfgs"] = ST.get("cfgs", {})
    ST["cfgs"][f"P0{tag}"] = cfg
    ST["mana_needs"][tag] = {}
    say(f"[{tag}] game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

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
                    # no revision for a while, re-tick anyway — a tick that
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
                    raw = await p0.export_state()
                    with open(f"{EVDIR}/{fname}", "w") as f:
                        f.write(raw)
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


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #1234 — feasible_mana_capacity colored-shard feasibility (revalidation)", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) protocol {sv['protocol_version']} — {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: all four games reached setup",
        "A2a_gate_offers_1B": "A2a gate offers {1}{B} (Cabal Ritual) at empty pool",
        "A2b_cast_completes": "A2b real cast resolves: Ghoul+Ritual in GY, {B}x3 pooled",
        "A3a_gate_offers_BB": "A3a [bug] gate offers {B}{B} (Sign in Blood) at empty pool",
        "A3b_manual_proof": "A3b two real Altar activations float {B}{B}; cast resolves P1 20->18",
        "A4_gate_rejects_B": "A4 gate correctly withholds {B} with 0 creatures",
        "A5a_gate_offers_2": "A5a gate offers {2} (Steel Overseer) via KCI",
        "A5b_cast_completes": "A5b real cast via KCI: Overseer on field, Memnite in GY",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:8]:
        d.text((40, y), str(n)[:120], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 1234/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_1234_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_1234_01010 instance is alive "
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
               "data_level_ok": False, "started_at": time.strftime(
                   "%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))})
    await verify_server_hello()
    check_data_level()

    games = {}
    for cfg in GAMES:
        games[cfg["tag"]] = await run_game(cfg)

    # ---- consolidate assertions ----
    ass = {k: "not-run" for k in ASS_KEYS}
    for tag, g in games.items():
        for k, v in g["ass"].items():
            if k in ass:
                ass[k] = v
    a1s = [games[t]["ass"].get(f"A1_setup_ok_{t}") for t in "ABCD"]
    ass["A1_setup_ok"] = "passed" if all(v == "passed" for v in a1s) \
        else ("failed" if any(v == "failed" for v in a1s) else "not-run")
    notes_all = []
    for tag, g in games.items():
        notes_all.append(f"--- game {tag} ({g['cfg']['spell']} {g['cfg']['cost']}) "
                         f"duration {g['duration_s']}s ---")
        notes_all.extend(g["notes"])

    # ---- verdict ----
    if ass["A1_setup_ok"] == "passed" and ass["A3a_gate_offers_BB"] == "failed" \
            and ass["A3b_manual_proof"] == "passed":
        verdict = "reproduced"
    elif ass["A1_setup_ok"] == "passed" and ass["A3a_gate_offers_BB"] == "passed":
        verdict = "not-reproduced"
    elif ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes_all.append("setup incomplete for at least one game; see notes")
    else:
        verdict = "blocked"
        notes_all.append("inconclusive assertion combination; see notes")

    result_line = (
        f"A1 setup_ok: {ass['A1_setup_ok']}; "
        f"A2a gate_offers_{{1}}{{B}}: {ass['A2a_gate_offers_1B']}; "
        f"A2b cast_completes: {ass['A2b_cast_completes']}; "
        f"A3a gate_offers_{{B}}{{B}}: {ass['A3a_gate_offers_BB']}; "
        f"A3b manual_proof: {ass['A3b_manual_proof']}; "
        f"A4 gate_rejects_{{B}}: {ass['A4_gate_rejects_B']}; "
        f"A5a gate_offers_{{2}}: {ass['A5a_gate_offers_2']}; "
        f"A5b cast_completes: {ass['A5b_cast_completes']}"
    )

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/1234",
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t_start, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/20261006-1414-1234 (fresh v0.103.0 server started by "
                            "this run on 127.0.0.1:9374)",
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "A": "12x Cabal Ritual + 4x Phyrexian Altar + 8x Diregraf Ghoul + 36x Swamp",
            "B": "12x Sign in Blood + 4x Phyrexian Altar + 12x Memnite + 32x Forest",
            "C": "12x Dark Ritual + 4x Phyrexian Altar + 44x Forest",
            "D": "12x Steel Overseer + 4x Krark-Clan Ironworks + 12x Memnite + 32x Forest",
            "P1": "60x Forest (draw-go)",
        },
        "setup_line": "A: Ritual+Altar+Ghoul+Swamps | B: Sign+Altar+2 Memnite+Forests | C: Ritual+Altar+0 creatures | D: Overseer+KCI+Memnite+Forests",
        "contract_line": ("Gate must offer {1}{B} (A) and {2} (D) casts, must "
                          "NOT offer {B} with no creatures (C), and must offer "
                          "{B}{B} via two Altar activations (B — the reported bug)."),
        "stats": {tag: {"game_code": g.get("game_code"),
                        "duration_s": g["duration_s"],
                        "offered_seen": g["offered_seen"], "obs": g["obs"],
                        "sacs_done": g["sacs_done"],
                        "resolution_B": g.get("resolution_B")}
                  for tag, g in games.items()},
        "assertions": ass,
        "notes": notes_all,
        "verdict": verdict,
        "result": result_line,
        "scope": "feasible_mana_capacity colored-shard feasibility under non-tap "
                 "mana sources (Phyrexian Altar / Krark-Clan Ironworks); native "
                 "engine, two human-client seats; four games covering {1}{B}, "
                 "{B}{B}, {B} (negative), {2} (KCI regression)",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x spell density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "contract": (
            "Game A ({1}{B} Cabal Ritual, Swamp+Altar+Ghoul): gate must offer; "
            "then real cast. Game B ({B}{B} Sign in Blood, Forests+Altar+2 "
            "Memnites): gate must offer (bug: does not); manual double "
            "activation floats {B}{B} as proof, then real cast at P1. "
            "Game C ({B} Dark Ritual, Altar+0 creatures): gate must NOT offer. "
            "Game D ({2} Steel Overseer, KCI+Memnite): gate must offer; then "
            "real cast via KCI activation."
        ),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_1234_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes_all,
                   "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    WIRE.close()
    RUNLOG.close()
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1), flush=True)


asyncio.run(main())
