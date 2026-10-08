#!/usr/bin/env python3
"""Issue #650: Tyvar the Bellicose — granted trigger does not work.

Re-validation on the pinned release v0.104.0 (build 4227122, protocol 118).
Prior runs: v0.77.0 (reproduced), v0.83.0 (blocked attempt), v0.86.0
(reproduced, protocol 72, run 20260917-650b), v0.101.0 (reproduced, protocol
103, run 20261004-650).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json, v0.104.0):
  Tyvar the Bellicose {2}{B}{G}, Legendary Creature — Elf Warrior 5/4.
  Oracle: "Whenever one or more Elves you control attack, they gain deathtouch
  until end of turn. Each creature you control has 'Whenever a mana ability of
  this creature resolves, put a number of +1/+1 counters on it equal to the
  amount of mana this creature produced. This ability triggers only once each
  turn.'"

Reported failure: tapping creatures for mana does not produce the counters;
the reporter saw the ability fire at most once per turn instead of once per
creature-tap (the granted instance is per-creature, once per turn).

Setup:
  P0: 12x Tyvar the Bellicose, 8x Llanowar Elves, 20x Forest, 20x Swamp
  (dense playsets in a custom game; the engine accepts >4-of).
  P1: 60x Island, draw-go.
  Both keep 7 (P0 mulligans toward an Elves opener, max 3).

Trigger (leg A): with Tyvar on the battlefield and a sickness-free untapped
Llanowar Elves A, P0 activates Elves A's mana ability ({T}: Add {G}) in its
own main phase while holding priority. Expected: the granted trigger resolves
and A ends with exactly 1 +1/+1 counter.

Trigger (leg B, same turn as leg A): activate a second sickness-free
Llanowar Elves B's mana ability on the same turn. Expected: B's own granted
trigger (a separate per-creature instance) resolves and B ends with exactly
1 +1/+1 counter. A per-turn-global throttle would leave B at 0.

Assertions:
  A1_setup_ok      — Tyvar + >=1 Elves on the battlefield by the trigger.
  A2_mana_produced — Elves A tapped and P0's green pool grew (the mana
                     ability itself resolved).
  A3_counters_added— Elves A carries exactly 1 +1/+1 counter after the
                     trigger window (failed = the reported bug).
  A4_per_creature  — same-turn Elves B carries exactly 1 +1/+1 counter
                     (not-run unless A3 passed: throttle unevaluable).

Driver conventions (protocol 118, from scenario_301_01040.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 plus a schema/select
    opportunity offering hand cards (106 uses a generic 'choose' code).
  - Mana-ability activation: merged_actions ActivateAbility (source_id match)
    first, else viewer_interaction exactChoices 'activateAbility' answered
    with {"type":"choose","data":{"choiceId":...}}. CRITICAL: the
    ActivateAbility submission must go out while the activating seat HOLDS
    priority — submitting after a priority pass leaves the ability
    unactivated (first 106 attempt stalled 70 turns with no exile).
  - Protocol-106 priority menus are noisy (tapLandForMana / untapLandForMana
    / castSpell / activateAbility offered at ordinary priority windows);
    real_decision_pending excludes those codes so passes are not blocked.
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    computing turn/in_main/hand and evaluating legs (leg-engagement race).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
  - deck schema {"main_deck": [...]}; client.py HELLO advertises 118.
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
RUN_ID = os.environ.get("RUN_ID", "run-650-reval-v01040-20261008-1511")
ISSUE = 650
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.104.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-08: latest stable release v0.104.0 (published "
               "2026-10-08) == pinned release dir; ServerHello "
               "0.104.0/4227122/protocol 118 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "reusing pinned v0.104.0 server on 127.0.0.1:9374 owned by the "
               "backfill loop (pid 6841, run-301-reval games.db); fresh game "
               "created by this run; isolated evidence under evidence/650/" + RUN_ID + "),"),
}

for _f, _k in (("server/releases/v0.104.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.104.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.104.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"))

TYVAR = "tyvar the bellicose"
ELVES = "llanowar elves"
FOREST, SWAMP, ISLAND = "forest", "swamp", "island"

P0_DECK = deck((TYVAR, 12), (ELVES, 8), (FOREST, 20), (SWAMP, 20))
P1_DECK = deck((ISLAND, 60))

SETUP_DEADLINE_S = 1500
ASS_KEYS = ("A1_setup_ok", "A2_mana_produced", "A3_counters_added",
            "A4_per_creature")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")


# ---------------------------------------------------------------- state helpers

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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


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


def can_pay(state, pid, colors=(), generic=0):
    color_of = {SWAMP: "B", ISLAND: "U", FOREST: "G"}
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


def mana_pool_g(state, pid):
    units = (player_of(state, pid).get("mana_pool") or {}).get("mana", [])
    return sum(1 for u in units if "green" in json.dumps(u).lower())


def plus_counters(obj):
    v = obj.get("counters")
    if isinstance(v, dict):
        n = 0
        for k, c in v.items():
            if str(k).upper().replace("_", "") in ("P1P1", "+1/+1", "PLUS1PLUS1"):
                n += c if isinstance(c, int) else 0
        return n, {"counters": v}
    if isinstance(v, int) and v:
        return v, {"counters": v}
    return 0, {"counters": v}


def my_priority(acts):
    """Protocol 118: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


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


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
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
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    ok, notes = True, []
    ty = CARD_DATA.get(TYVAR, {})
    if "Whenever a mana ability of this creature resolves" not in str(ty.get("oracle_text")):
        ok = False
        notes.append("Tyvar granted-trigger oracle shape missing")
    le = CARD_DATA.get(ELVES, {})
    if "Add {G}" not in str(le.get("oracle_text")):
        ok = False
        notes.append("Llanowar Elves mana ability oracle missing")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "tyvar_oracle": str(ty.get("oracle_text"))[:260],
                   "elves_oracle": str(le.get("oracle_text"))[:160]}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 118: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission). P0 mulligans toward
    an Elves/Tyvar opener (max 3); P1 always keeps."""
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
    if key in MULLS:
        return False
    state = st["state"]
    hn = hand_lnames(state, pid)
    MULLS.add(key)
    if tag == "P0":
        keep = ELVES in hn or TYVAR in hn
        mull_count = sum(1 for k in MULLS if k[0] == "P0")
        if len(hn) > 4 and not keep and mull_count < 4:
            say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
            return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 118: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity.
    Gated on kind == 'mulligan' AND turn 1 / Untap so it cannot misfire on
    mid-game SelectCards prompts."""
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in (TYVAR, ELVES):
            return (1, str(ref))  # keep these on top
        return (0, str(ref))      # bottom everything else first

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 118: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards
    (106 uses a generic 'choose' waitingForKind code)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    found_opp = None
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
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"
    bf_elves = len(bf_by_name(state, pid, ELVES))
    bf_tyvar = len(bf_by_name(state, pid, TYVAR))
    hn_elves = sum(1 for o in hand if obj_lname(state, o) == ELVES)

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else choice_text(ch).lower()
        if nm in (SWAMP, FOREST):
            return (0, nm)
        if nm == TYVAR and bf_tyvar:
            return (1, nm)
        if nm == ELVES and (bf_elves >= 2 or
                            (bf_elves >= 1 and hn_elves > 1)):
            return (2, nm)
        return (5, nm)

    picks = [ch["id"] for ch in sorted(cands, key=rank)[:max(1, n)]]
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


async def try_cast(c, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    return True


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
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
        if sum(needs.values()) <= 0:
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


def find_activate(st, source_oid):
    """Elves mana ability on protocol 106: merged_actions ActivateAbility
    (source_id match) first; else viewer_interaction exactChoices
    'activateAbility' answered with {"type":"choose"}. legal_actions_by_object
    is still present on 106 (merged_actions unchanged)."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility" and \
                str((a.get("data") or {}).get("source_id")) == str(source_oid):
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


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
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
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def export_tag(c, name):
    raw = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(raw)
    say(f"exported {name}.json")


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
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
    if "ChooseLegend" in atypes:
        da = next((a for a in acts if a["type"] == "ChooseLegend"), None)
        if da:
            await submit_as_is(c, da)
            say(f"[P0] ChooseLegend keep-first")
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return

    # ---- priority gate, then yield before leg evaluation (leg-engagement race fix)
    gate_done = False
    if my_priority(acts):
        gate_done = True
    await asyncio.sleep(0)  # yield to the pump before reading fresh state
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_priority(acts) and ST["phase"] == "settle":
        await pass_priority(c, st, acts)
        return

    turn = state.get("turn_number", 0)
    in_main = my_main(state, pid)
    my_turn = state.get("active_player") == 0
    has_prio = my_priority(acts)
    tyvar_oids = bf_by_name(state, 0, TYVAR)
    tyvar_id = tyvar_oids[0] if tyvar_oids else None
    elves_oids = bf_by_name(state, 0, ELVES)
    hn = hand_lnames(state, 0)
    for eo_id in elves_oids:
        ST["elves_seen_turn"].setdefault(eo_id, turn)

    if tyvar_id and elves_oids and ST["ass"]["A1_setup_ok"] == "not-run":
        ST["ass"]["A1_setup_ok"] = "passed"
        say(f"setup ok at turn {turn} (Tyvar + {len(elves_oids)} Elves)")

    def sickness_free(oid):
        return turn > ST["elves_seen_turn"].get(oid, turn)

    # failed-interaction retry: a submitted activation that moves no
    # revision in 12s is unanswered — retry.
    if (ST["activated"] and ST["need_mid"] and ST["interaction_sent_at"]
            and time.time() - ST["interaction_sent_at"] > 12):
        ST["notes"].append(f"interaction at turn {ST['pre_turn']} unanswered "
                           "(no revision in 12s); retrying activation")
        say("interaction unanswered; resetting leg-A activation to retry")
        ST["activated"] = False
        ST["need_mid"] = False
        ST["interaction_sent_at"] = None
        ST["elves_a"] = None

    # ---- leg A: activate first sickness-free Elves (while holding priority)
    if (tyvar_id and in_main and my_turn and has_prio and not ST["activated"]
            and not ST["finished"]):
        cand = next((o for o in elves_oids
                     if sickness_free(o) and not get_obj(state, o).get("tapped")),
                    None)
        if cand:
            r = find_activate(st, cand)
            if r:
                kind, sub, what = r
                ST["elves_a"] = cand
                ST["pre_turn"] = turn
                ST["pre_pool_g"] = mana_pool_g(state, 0)
                say(f"PRE export at turn {turn}; activating Elves A mana "
                    f"ability (oid {cand}) via {what}")
                await export_tag(c, "pre")
                if kind == "interaction":
                    opp, ch = sub
                    await answer_vi(c, opp, ch, tag)
                else:
                    await submit_as_is(c, sub)
                ST["activated"] = True
                ST["need_mid"] = True
                ST["interaction_sent_at"] = time.time()
                wire("legA_activate", {"oid": cand, "via": what})
                return

    # mid checkpoint: prove the mana ability resolved
    if ST["need_mid"] and pid == 0 and has_prio:
        await export_tag(c, "mid")
        env = json.load(open(f"{EVDIR}/mid.json"))
        eo = get_obj(env["state"], ST["elves_a"])
        pool_g = mana_pool_g(env["state"], 0)
        ST["notes"].append(f"mid: Elves A tapped={eo.get('tapped')} "
                           f"pool_g={pool_g} (pre pool_g={ST['pre_pool_g']})")
        ST["ass"]["A2_mana_produced"] = "passed" if (
            eo.get("tapped") and pool_g > (ST["pre_pool_g"] or 0)) else "failed"
        say(f"A2 = {ST['ass']['A2_mana_produced']} "
            f"(tapped={eo.get('tapped')} pool {ST['pre_pool_g']}->{pool_g})")
        wire("A2", {"tapped": eo.get("tapped"), "pre_pool": ST["pre_pool_g"],
                    "post_pool": pool_g})
        ST["need_mid"] = False
        # NOTE: no return here — fall through so this same tick still
        # counts the trigger window and passes priority; returning would
        # stall the game (no new revision arrives while P0 holds priority).

    # leg-A trigger window
    if ST["activated"] and not ST["a_done"] and not ST["need_mid"] \
            and not ST["finished"]:
        ST["post_rounds"] += 1
        if ST["post_rounds"] >= 40:
            await export_tag(c, "post")
            env = json.load(open(f"{EVDIR}/post.json"))
            eo = get_obj(env["state"], ST["elves_a"])
            n, sample = plus_counters(eo)
            ST["notes"].append(f"post: Elves A counters raw sample: "
                               f"{json.dumps(sample)[:300]}")
            ST["notes"].append(f"post: Elves A tapped={eo.get('tapped')}")
            wire("post_counters", {"oid": ST["elves_a"], "n": n,
                                   "sample": sample})
            if n == 1:
                ST["ass"]["A3_counters_added"] = "passed"
            elif n == 0:
                ST["ass"]["A3_counters_added"] = "failed"
                ST["notes"].append("BUG: no +1/+1 counters on Elves A after "
                                   "granted trigger window")
            else:
                ST["ass"]["A3_counters_added"] = "failed"
                ST["notes"].append(f"unexpected counter count on Elves A: {n}")
            say(f"A3 = {ST['ass']['A3_counters_added']} (counters={n})")
            ST["a_done"] = True

    # ---- leg B: same-turn second creature ----
    if (ST["a_done"] and ST["ass"]["A3_counters_added"] == "passed"
            and not ST["b_done"] and not ST["finished"]):
        if turn > ST["pre_turn"]:
            ST["ass"]["A4_per_creature"] = "not-run"
            ST["notes"].append("A4 not-run: turn advanced past leg-A turn "
                               "before leg-B activation")
            ST["b_done"] = True
        elif not ST["b_activated"] and in_main and my_turn and has_prio:
            cand = next((o for o in elves_oids
                         if o != ST["elves_a"] and sickness_free(o)
                         and not get_obj(state, o).get("tapped")), None)
            if cand:
                r = find_activate(st, cand)
                if r:
                    kind, sub, what = r
                    ST["elves_b"] = cand
                    ST["b_turn"] = turn
                    say(f"PRE2 export at turn {turn}; activating Elves B "
                        f"(oid {cand}) via {what}")
                    await export_tag(c, "pre2")
                    if kind == "interaction":
                        opp, ch = sub
                        await answer_vi(c, opp, ch, tag)
                    else:
                        await submit_as_is(c, sub)
                    ST["b_activated"] = True
                    wire("legB_activate", {"oid": cand, "via": what})
                    return
        if ST["b_activated"]:
            ST["post_rounds2"] += 1
            if ST["post_rounds2"] >= 40:
                await export_tag(c, "post2")
                env = json.load(open(f"{EVDIR}/post2.json"))
                eo = get_obj(env["state"], ST["elves_b"])
                n, sample = plus_counters(eo)
                ST["notes"].append(f"post2: Elves B counters raw sample: "
                                   f"{json.dumps(sample)[:300]}")
                wire("post2_counters", {"oid": ST["elves_b"], "n": n,
                                        "sample": sample})
                if n == 1:
                    ST["ass"]["A4_per_creature"] = "passed"
                elif n == 0:
                    ST["ass"]["A4_per_creature"] = "failed"
                    ST["notes"].append("BUG: no +1/+1 counters on Elves B "
                                       "after same-turn activation "
                                       "(global once-per-turn throttle?)")
                else:
                    ST["ass"]["A4_per_creature"] = "failed"
                    ST["notes"].append(f"unexpected counter count on Elves B: {n}")
                say(f"A4 = {ST['ass']['A4_per_creature']} (counters={n})")
                ST["b_done"] = True
                ST["finished"] = True

    # if leg-A bug confirmed, leg B is moot
    if (ST["a_done"] and ST["ass"]["A3_counters_added"] == "failed"
            and not ST["finished"]):
        ST["ass"]["A4_per_creature"] = "not-run"
        ST["notes"].append("A4 not-run: granted trigger never fires (A3 "
                           "failed), per-creature throttling cannot be evaluated")
        ST["finished"] = True

    if ST["finished"]:
        return

    # main-phase economy: land first, then cast (cap Elves at 2, skip 2nd Tyvar)
    if in_main and my_turn:
        acted = False
        if await play_a_land(c, state, pid, acts, tag):
            return
        wants = []
        if len(elves_oids) < 2:
            wants.append(ELVES)
        if not tyvar_id:
            wants.append(TYVAR)
        for want in wants:
            if want in hn:
                colors, gen = (("G",), 0) if want == ELVES else (("B", "G"), 2)
                needs = {c_: 1 for c_ in colors}
                needs["generic"] = gen
                if can_pay(state, pid, colors, gen):
                    if await try_cast(c, state, acts, tag, want, needs):
                        return
    if has_prio:
        await pass_priority(c, st, acts)


async def main():
    pidfile = "/tmp/scenario_650_01040.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_650_01040 instance is alive "
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
    ST.update({
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "tick": 0,
        "phase": "setup",
        "elves_seen_turn": {},
        "pre_turn": None,
        "b_turn": None,
        "elves_a": None,
        "elves_b": None,
        "activated": False,
        "need_mid": False,
        "post_rounds": 0,
        "a_done": False,
        "b_activated": False,
        "post_rounds2": 0,
        "b_done": False,
        "finished": False,
        "pre_pool_g": None,
        "mulls": {0: 0, 1: 0},
        "interaction_sent_at": None,
        "game_code": None,
    })
    await verify_server_hello()
    check_data_level()

    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    i = 0
    while i < 6000:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
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
                        f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                        f"holding_priority={my_priority(merged_actions(st))}")
                    last_change[pid] = time.time()
                # Safety net: if this client holds priority but produced no
                # revision for a while, re-tick anyway — a tick that
                # returned without submitting must not stall the game.
                holds_prio = my_priority(merged_actions(st))
                if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st = p0.latest
        if st:
            max_turn = max(max_turn, st["state"].get("turn_number", 0))
        if ST["finished"]:
            break
        if max_turn > 40 and not ST["activated"]:
            ST["notes"].append("could not reach leg-A trigger by turn 40 "
                               "(Tyvar/Elves never both deployed)")
            break
        if time.time() - t0 > SETUP_DEADLINE_S and not ST["finished"]:
            ST["notes"].append("setup deadline hit")
            break

    ass = ST["ass"]
    if ass["A1_setup_ok"] == "not-run":
        ass["A1_setup_ok"] = "failed" if max_turn > 40 else "not-run"
    if not ST["activated"]:
        ass["A2_mana_produced"] = "not-run"
        ass["A3_counters_added"] = "not-run"
        ass["A4_per_creature"] = "not-run"
        ST["notes"].append("leg-A activation never happened")
    if ST["a_done"] and ass["A3_counters_added"] == "passed" \
            and not ST["b_done"] and not ST["b_activated"]:
        ass["A4_per_creature"] = "not-run"
        ST["notes"].append("A4 not-run: no second sickness-free Elves "
                           "available on the leg-A turn")

    # parser corroboration against pinned card-data
    try:
        ty = CARD_DATA.get(TYVAR, {})
        texts = json.dumps(ty.get("abilities", ty))[:400]
        ST["notes"].append("parser corroboration: card-data.json v0.104.0 "
                           f"Tyvar entry abilities excerpt: {texts}")
    except Exception as e:
        ST["notes"].append(f"parser check skipped: {e}")

    a1, a2, a3, a4 = (ass["A1_setup_ok"], ass["A2_mana_produced"],
                      ass["A3_counters_added"], ass["A4_per_creature"])
    if a1 == "passed" and a2 == "passed":
        if a3 == "failed" or a4 == "failed":
            verdict = "reproduced"
        elif a3 == "passed" and a4 == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/650",
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "reusing pinned v0.104.0 server on 127.0.0.1:9374 (pid 6841, games.db runs/run-301-reval-v01040-20261008-1443); fresh game by this run",
        "driver": {"protocol_advertised": 118, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": "12x Tyvar the Bellicose + 8x Llanowar Elves + 20x Forest + 20x Swamp",
                  "P1": "60x Island"},
        "setup_line": "P0: 12x Tyvar the Bellicose + 8x Llanowar Elves + 20 Forest + 20 Swamp | P1: 60 Island (draw-go)",
        "contract_line": "Tap Elves for {G} with Tyvar out: granted trigger must put 1 +1/+1 counter on the tapped creature (once per turn, per creature).",
        "stats": {"max_turn": max_turn, "pre_turn": ST["pre_turn"],
                  "b_turn": ST["b_turn"], "activated": ST["activated"],
                  "b_activated": ST["b_activated"]},
        "assertions": ass,
        "notes": ST["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_650_01040.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)

    # AGENTS.md lesson: close WIRE/RUNLOG BEFORE the manifest loop; the final
    # verdict lines flush after the hashes would otherwise fail sha256sum -c.
    say(f"closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1), flush=True)
    await p0.close()
    await p1.close()


asyncio.run(main())
