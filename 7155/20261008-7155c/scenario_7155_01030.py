#!/usr/bin/env python3
"""Issue #7155 re-validation on v0.103.0 (protocol 106): Fiend Artisan.

Report: "Game does not let you pay for Fiend Artisan's ability."
Oracle: This creature gets +1/+1 for each creature card in your graveyard.
  {X}{B/G}, {T}, Sacrifice another creature: Search your library for a
  creature card with mana value X or less, put it onto the battlefield,
  then shuffle. Activate only as a sorcery.

Prior v0.82.0 (protocol 70) run verdict: reproduced -- the engine-advertised
ManaPayment finalize (passPriority choice, CR 601.2h) was rejected
action_not_allowed; waiting_for stuck at ManaPayment; Artisan untapped, no
sacrifice, no search, ability never resolves.

Behavioral contract (2 human seats, native engine):
  P0 fields Fiend Artisan (untapped, past summoning sickness), 2x Llanowar
  Elves fodder, 4x Grizzly Bears in the library, lands to pay {2}{B/G}.
  On a P0 main phase with an empty stack, P0 activates the ability, answers
  the X prompt with X=2, pays {2}{B/G}, taps, sacrifices an Elves, then
  answers the library search by picking Grizzly Bears (MV 2 <= X).
  A1 parse_cost:  card-data cost AST = Mana{X,B/G} + Tap + Sacrifice
                  (Another Creature); effect search carries X-bound Cmc.
  A2 setup_ok:    Artisan on P0 BF untapped & attack-ready; >=1 Elves
                  on P0 BF; >=3 untapped lands able to produce {2}{B/G}.
  A3 activation_offered: ActivateAbility(source=Artisan) advertised to P0
                  on a main phase with empty stack.
  A4 x_prompt:    engine prompts for X; driver answers X=2 and the
                  prompt advances (not stalled).
  A5 cost_paid:   {2}{B/G} paid + Artisan tapped + one Elves sacrificed.
  A6 search_prompt: library search offered; Bears (MV 2 <= 2) offered.
  A7 resolution:  Bears enters P0 BF; Artisan ability leaves the stack.
  A8 cleanup:     stack empty, no stall, game advanced past the test.

Verdict: reproduced iff A2 passes and any of A3-A7 fails (the reported
"cannot pay for the ability" shape). not-reproduced iff A2-A8 all pass.
blocked iff A2 cannot be established.

Protocol-106 driver notes (v0.103.0): ported from scenario_301_01030.py
conventions -- my_priority = PassPriority in legal_actions; casts are gated
on it and use the engine's Auto payment (the driver never taps mana for
casts); activated-ability cost payment is driven via the vi tapLandForMana
choices only when offered by the engine (the 70-era run showed the engine
asks; if the engine auto-pays, no choices are offered and the needs are
simply zeroed once the cost is observed paid); X prompts are answered by
numeric exactChoices; sacrifice and search prompts are schema/select or
exactChoices with object references; the stack-watch branch always falls
through to the priority-pass gate (both seats must pass in succession for
a stack entry to resolve on 106).
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario7155_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261008-7155c"
ISSUE = 7155
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ARTISAN, ELVES, BEARS = "fiend artisan", "llanowar elves", "grizzly bears"
SWAMP, FOREST = "Swamp", "Forest"
LANDS = (SWAMP, FOREST, "Plains", "Island", "Mountain")
X_VALUE = 2

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
                       "in prehashed (blake2b-512) mode at pin time; data "
                       "digests match the signed manifest; digests recomputed "
                       "against on-disk files this run"),
    "source": ("2026-10-08: latest stable release v0.103.0 == pinned release "
               "dir; ServerHello 0.103.0/ec27a8d/protocol 106 verified by "
               "handshake this run; hashes recomputed against on-disk "
               "artifacts this run; reused the already-listening v0.103.0 "
               "single-user server on 127.0.0.1:9374 (pid 4471, started by "
               "run 20261008-7148 -- not replaced, per playbook)"),
}

for _f, _k in (("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.103.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}, "rejections": []}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


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
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def lib_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]


def gy_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller", -1)) == str(pid)]


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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
            if isinstance(s.get("data"), dict)]


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
        msg = {"type": a["type"], "data": copy.deepcopy(a["data"])}
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
    assert str(ver) in ("0.103.0", "v0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


def check_parse():
    fa = CARD_DATA.get("fiend artisan") or {}
    acts = [a for a in fa.get("abilities", []) if a.get("kind") == "Activated"]
    ok, detail = False, ""
    if acts:
        a = acts[0]
        cost = a.get("cost") or {}
        costs = cost.get("costs") or []
        kinds = [c.get("type") for c in costs]
        mana = next((c for c in costs if c.get("type") == "Mana"), None)
        shards = ((mana or {}).get("cost") or {}).get("shards") or []
        has_tap = "Tap" in kinds
        sac = next((c for c in costs if c.get("type") == "Sacrifice"), None)
        sac_t = ((sac or {}).get("target") or {})
        sac_props = (sac_t.get("properties") or [])
        sac_ok = (sac_t.get("type_filters") == ["Creature"]
                  and any(p.get("type") == "Another" for p in sac_props))
        eff = a.get("effect") or {}
        filt = eff.get("filter") or {}
        props = filt.get("properties") or []
        cmc = next((p for p in props if p.get("type") == "Cmc"), None)
        cmc_val = (cmc or {}).get("value") or {}
        cmc_qty = cmc_val.get("qty") or {}
        cmc_ok = (eff.get("type") == "SearchLibrary"
                  and filt.get("type") == "Typed"
                  and (cmc or {}).get("comparator") == "LE"
                  and cmc_val.get("type") == "Ref"
                  and cmc_qty.get("type") == "Variable")
        sub = a.get("sub_ability") or {}
        sub_ok = (sub.get("effect") or {}).get("type") == "ChangeZone"
        unimp = "Unimplemented" in json.dumps(a)
        ok = (set(shards) == {"X", "BlackGreen"} and has_tap and sac_ok
              and cmc_ok and sub_ok and not unimp)
        detail = (f"shards={shards} tap={has_tap} sac_ok={sac_ok} "
                  f"cmc_x_bound={cmc_ok} sub_bf={sub_ok} "
                  f"unimplemented={unimp}")
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_check", {"ok": ok, "detail": detail,
                         "ability": acts[0] if acts else None})
    with open(f"{EVDIR}/parse_fiend_artisan.json", "w") as f:
        json.dump({"ok": ok, "detail": detail,
                   "ability": acts[0] if acts else None}, f, indent=1,
                  default=str)
    return "passed" if ok else "failed"


async def do_mulligan(c, acts, st, pid, tag, protect=None):
    """Protocol 106: MulliganDecision arrives as a legacy legal action."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", c.revision)
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    decision = "Keep"
    if pid == 0 and not any(n == ARTISAN for n in hn):
        decision = "Mulligan"
        say(f"[{tag}] mulliganing (no Artisan in {hn})")
    else:
        say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": decision}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == ARTISAN:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    picks = sorted(cands, key=bkey)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id")
                                                              for ch in picks]}}},
                      tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
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
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm in (ARTISAN, BEARS, ELVES):
            return (5, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
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
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    """Answer vi tapLandForMana payment prompts (protocol 106). `needs` is
    a dict {color: count, "generic": count} mutated as colored needs are
    satisfied. Returns True if a tap was submitted."""
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
        say(f"[{tag}] tap land for mana used_for={used} "
            f"(needs now {needs if needs is not None else 'n/a'})")
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


def cast_action_for(acts, state, lname):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == lname:
                    return a, iv
    return None, None


def find_activate(st, source_oid):
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


# ---------------------------------------------------------------- cost-prompt handlers

def _num_of(ch):
    for cand in (choice_text(ch),):
        try:
            return int(str(cand).strip())
        except Exception:
            pass
    return None


async def handle_x_value(c, tag, st, state, acts):
    """Answer the {X} choice prompt with X=X_VALUE. Only fires on a pure
    numeric menu (every offered choice parses as an integer) while the
    activation cost is being paid -- in this scenario that shape is
    unambiguous."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        rtype = resp.get("type")
        spec = data.get("spec", {}) or {}
        if rtype == "schema":
            stype = spec.get("type")
            if stype != "number":
                continue
            iid = opp.get("interactionId")
            key = (tag, "x", iid)
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] X prompt (schema number); answering X={X_VALUE}")
            wire("x_prompt", {"shape": "schema-number"})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "number",
                                               "data": {"value": X_VALUE}}},
                              tag)
            ST["x_answered"] = True
            ST["x_rej_mark"] = len(ST["rejections"])
            return True
        if not chs:
            continue
        if rtype != "exactChoices":
            continue
        nums = [_num_of(ch) for ch in chs]
        if any(n is None for n in nums):
            continue
        iid = opp.get("interactionId")
        key = (tag, "x", iid)
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        want = None
        for ch, n in zip(chs, nums):
            if n == X_VALUE:
                want = ch
                break
        if want is None:
            say(f"[{tag}] X menu offers {[n for n in nums]}; "
                f"no {X_VALUE} -- not answering")
            wire("x_prompt_no_match", {"options": nums})
            continue
        say(f"[{tag}] X prompt (numeric menu {nums}); answering X={X_VALUE}")
        wire("x_prompt", {"shape": "numeric-menu", "options": nums})
        await answer_vi(c, opp, want, tag)
        ST["x_answered"] = True
        ST["x_rej_mark"] = len(ST["rejections"])
        return True
    return False


async def handle_sacrifice(c, tag, st, state):
    """Answer the sacrifice-cost selection with a Llanowar Elves (never the
    Artisan). Handles schema/select candidates and exactChoices with object
    references."""
    if ST.get("sac_submitted"):
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = [_cand_reference(ch) for ch in items]
        if not any(r is not None for r in refs):
            continue
        objs = [get_obj(state, r) for r in refs]
        if not all(o.get("zone") == "Battlefield"
                   and str(o.get("controller", -1)) == "0" for o in objs):
            continue
        iid = opp.get("interactionId")
        key = (tag, "sac", iid)
        if key in SUBMITTED_OPPS:
            return True
        want = None
        for ch, r in zip(items, refs):
            if r is not None and obj_lname(state, r) == ELVES:
                want = ch
                break
        if want is None:
            # pick any battlefield creature that is not the Artisan
            for ch, r in zip(items, refs):
                if r is not None and obj_lname(state, r) != ARTISAN:
                    want = ch
                    break
        if want is None:
            continue
        SUBMITTED_OPPS.add(key)
        ST["sac_submitted"] = True
        ST["sac_target_oid"] = str(_cand_reference(want))
        say(f"[{tag}] sacrifice prompt ({rtype}); sacrificing "
            f"oid={ST['sac_target_oid']} "
            f"({obj_lname(state, ST['sac_target_oid'])})")
        wire("sacrifice_prompt", {"rtype": rtype,
                                  "iid": iid,
                                  "pick": ST["sac_target_oid"]})
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type") or "select"
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": stype,
                                               "data": {"choiceIds": [want.get("id")]}}},
                              tag)
        else:
            await answer_vi(c, opp, want, tag)
        return True
    return False


async def handle_search(c, tag, st, state):
    """Answer the library search by picking Grizzly Bears (MV 2 <= X=2).
    Candidates must reference Library-zone objects of P0."""
    if ST.get("search_answered"):
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = [_cand_reference(ch) for ch in items]
        if not any(r is not None for r in refs):
            continue
        objs = [get_obj(state, r) for r in refs]
        if not any(o.get("zone") == "Library"
                   and str(o.get("controller", -1)) == "0" for o in objs):
            continue
        iid = opp.get("interactionId")
        key = (tag, "search", iid)
        if key in SUBMITTED_OPPS:
            return True
        want = None
        for ch, r in zip(items, refs):
            if r is not None and obj_lname(state, r) == BEARS:
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: Bears not among search candidates "
                f"(n={len(items)}); not answering")
            wire("search_no_bears",
                 {"n": len(items),
                  "sample": [obj_lname(state, r) for r in refs[:8]
                             if r is not None]})
            continue
        SUBMITTED_OPPS.add(key)
        ST["search_answered"] = True
        say(f"[{tag}] search prompt ({rtype}); picking Grizzly Bears")
        wire("search_prompt", {"rtype": rtype, "iid": iid})
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type") or "select"
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": stype,
                                               "data": {"choiceIds": [want.get("id")]}}},
                              tag)
        else:
            await answer_vi(c, opp, want, tag)
        return True
    return False


# ---------------------------------------------------------------- ticks

def drain_rejections(c, tag):
    for r in c.rejections:
        if r not in ST["rejections"]:
            ST["rejections"].append({"who": tag, **r})
            wire("rejection", {"who": tag, "type": r.get("type"),
                               "data": json.dumps(r.get("data"),
                                                  default=str)[:500]})
            say(f"[{tag}] {r.get('type')}: "
                f"{json.dumps(r.get('data'), default=str)[:300]}")
    c.rejections.clear()

async def p1_tick(c, g, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
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
    drain_rejections(c, tag)
    # legacy PayMana actions are answered only when mana needs are
    # outstanding (gated) so they can't fire for Auto-paid test casts.
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_tick(c, acts):
            return
        if await pay_mana_vi(c, st, tag, needs):
            return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick_ramp(c, g, tag):
    """Ramp tick for P0. Returns ('ACTIVATE', artisan_oid) at the window."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True
    drain_rejections(c, tag)
    # casts on 106 use the engine's Auto payment: no driver-side mana taps.
    if my_main(state, 0):
        await asyncio.sleep(0)  # yield before leg evaluation (race fix)
        st = c.latest or st
        state = st["state"]
        acts = merged_actions(st)
        if await play_a_land(c, state, 0, acts, tag):
            return True
        artisan = bf_by_name(state, 0, ARTISAN)
        if not artisan:
            ao = next((o for o in hand_ids(state, 0)
                       if obj_lname(state, o) == ARTISAN), None)
            a, oid = cast_action_for(acts, state, ARTISAN)
            if a is not None:
                ST["artisan_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] casting Fiend Artisan (oid {oid}, "
                    f"engine Auto payment)")
                wire("cast_artisan", {"oid": oid})
                await submit_as_is(c, a)
                return True
        else:
            # cast Elves fodder (need >= 2 for the activation + control)
            if len(bf_by_name(state, 0, ELVES)) < 2:
                eo = next((o for o in hand_ids(state, 0)
                           if obj_lname(state, o) == ELVES), None)
                a, oid = cast_action_for(acts, state, ELVES)
                if a is not None:
                    say(f"[{tag}] casting Llanowar Elves (oid {oid})")
                    wire("cast_elves", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
        # --- activation window --------------------------------------
        art = bf_by_name(state, 0, ARTISAN)
        if art:
            oid = art[0]
            ao = get_obj(state, oid)
            unt = untapped_lands(state, 0)
            unt_bg = [o for o in unt
                      if obj_lname(state, o) in ("swamp", "forest")]
            ready = (not ao.get("tapped")
                     and state.get("turn_number", 0)
                     > (ST.get("artisan_cast_turn") or 0))
            elves = bf_by_name(state, 0, ELVES)
            if ready and len(elves) >= 1 and len(unt) >= X_VALUE + 1 \
                    and len(unt_bg) >= 1 and my_priority(acts):
                found = None
                for a in acts:
                    if a.get("type") == "ActivateAbility" and str(
                            a.get("data", {}).get("source_id")) == str(oid):
                        found = a
                        break
                if found:
                    ST["activation_offered"] = True
                    wire("offer_scan", {"offered": True,
                                        "untapped": len(unt),
                                        "untapped_bg": len(unt_bg),
                                        "elves": len(elves)})
                    say(f"[{tag}] ACTIVATE window: ActivateAbility offered "
                        f"(untapped={len(unt)} bg={len(unt_bg)} elves={len(elves)})")
                    return ("ACTIVATE", oid, found)
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def ability_on_stack(state):
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str).lower()
        if "fiend artisan" in blob and "ctivat" in blob:
            return True
    return False


async def activate_artisan(p0, p1, g, tag, artisan_oid, action, timeout=300):
    """Submit the advertised ActivateAbility WHILE HOLDING priority (never
    pass first), then drive the cost prompts: X value, mana payment, the tap
    (engine), and the sacrifice. Returns True once the ability is on the
    stack with its cost paid."""
    say(f"[{tag}] submitting ActivateAbility (holding priority)")
    wire("activate_ability", {"action": {k: v for k, v in action.items()
                                        if not k.startswith("_")}})
    ST["mana_needs"][tag] = {"G": 1, "generic": X_VALUE}
    ST["activation_attempts"] = ST.get("activation_attempts", 0) + 1
    ST["stage"] = "ACTIVATE"
    await submit_as_is(p0, action)
    rej_mark = len(ST["rejections"])
    x_submitted_at = None
    legacy_tried = False
    t0 = time.time()
    diag_n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        # P1 only passes; P0 must keep priority while the cost is pending.
        await p1_tick(p1, g, f"P1{tag}")
        st = p0.latest
        if st is None:
            continue
        state = st["state"]
        acts = merged_actions(st)
        drain_rejections(p0, tag)
        # --- X-rejection watch (before the handlers: a submitted X must
        # --- not be re-submitted, but its rejection must be observed) ---
        if ST.get("x_answered") and not ST.get("x_rejection_checked"):
            mark = ST.get("x_rej_mark", rej_mark)
            new_rej = [r for r in ST["rejections"][mark:]
                       if r.get("type") == "ActionRejected"]
            if new_rej:
                ST["x_rejection_checked"] = True
                ST["x_interaction_rejected"] = True
                # the cost can never be paid now; clear mana needs so the
                # driver doesn't tap lands for a dead payment (keeps the
                # tapped-land delta evidence clean)
                ST["mana_needs"][tag] = {}
                say(f"[{tag}] DECISIVE: wire-correct Number{{2}} interaction "
                    f"rejected: {json.dumps(new_rej[0].get('data'), default=str)[:200]}")
                wire("x_interaction_rejected", {"rejection": new_rej[0]})
        # --- explicit cost-prompt handlers first ---------------------
        if await handle_x_value(p0, tag, st, state, acts):
            if x_submitted_at is None and ST.get("x_answered"):
                x_submitted_at = time.time()
            # fall through to the rejection watch below instead of
            # continuing: the X submission may already have been rejected
        # --- X-rejection follow-up: try legacy ChooseX once, then fail fast
        if ST.get("x_interaction_rejected") and not legacy_tried:
            # X prompt still pending? try the legacy ChooseX action once.
            still = any(
                (op.get("response") or {}).get("type") == "schema"
                and ((op.get("response") or {}).get("data") or {})
                .get("spec", {}).get("type") == "number"
                for op in vi_ops(st))
            if still:
                legacy_tried = True
                rej_mark2 = len(ST["rejections"])
                say(f"[{tag}] trying legacy ChooseX{{{X_VALUE}}} action "
                    f"(the real client's path)")
                wire("legacy_choosex_submit", {"value": X_VALUE})
                await p0.send_action({"type": "ChooseX",
                                      "data": {"value": X_VALUE}})
                await asyncio.sleep(3)
                drain_rejections(p0, tag)
                new_rej2 = [r for r in ST["rejections"][rej_mark2:]
                            if r.get("type") == "ActionRejected"]
                if new_rej2:
                    ST["x_legacy_rejected"] = True
                    say(f"[{tag}] DECISIVE: legacy ChooseX{{{X_VALUE}}} "
                        f"rejected: {json.dumps(new_rej2[0].get('data'), default=str)[:200]}")
                    wire("x_legacy_rejected", {"rejection": new_rej2[0]})
                    await export_now(p0, "mid_cost.json")
                    ST["done_reason"] = ("X prompt advertised but both the "
                                         "wire-correct Number{2} interaction "
                                         "and the legacy ChooseX{2} action "
                                         "rejected action_not_allowed")
                    return False
        # fail fast: X answered >45s ago, prompt still pending, no advance
        if (ST.get("x_answered") and x_submitted_at is not None
                and time.time() - x_submitted_at > 45
                and not ST.get("cost_seen_paid")):
            say(f"[{tag}] X answered 45s ago, prompt still pending, cost "
                f"never paid -- failing fast")
            wire("x_stall_failfast", {})
            await export_now(p0, "mid_cost.json")
            ST["done_reason"] = ("X submission not advancing after 45s; "
                                 "cost never paid")
            return False
        needs = ST["mana_needs"].get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(p0, st, tag, needs):
                continue
        if await handle_sacrifice(p0, tag, st, state):
            continue
        if await handle_search(p0, tag, st, state):
            continue
        # --- cost-paid watch -----------------------------------------
        ao = get_obj(state, artisan_oid)
        sac_oids = [o for o in gy_oids(state, 0)
                    if obj_lname(state, o) == ELVES]
        unt = len(untapped_lands(state, 0))
        if ao.get("tapped") and sac_oids and not ST.get("cost_seen_paid"):
            ST["cost_seen_paid"] = True
            ST["mana_needs"][tag] = {}
            say(f"[{tag}] cost paid: Artisan tapped, Elves in GY "
                f"({sac_oids[0]}), untapped lands now {unt}")
            wire("cost_paid", {"elves_gy": sac_oids, "untapped": unt})
        if diag_n % 8 == 0:
            say(f"[{tag}] activation watch: tapped={ao.get('tapped')} "
                f"stack={len(state.get('stack') or [])} "
                f"x={ST.get('x_answered')} sac={ST.get('sac_submitted')} "
                f"needs={ST['mana_needs'].get(tag)} vikind={vi_kind_code(st)!r} "
                f"real_decision={real_decision_pending(st)}")
            wire(f"{tag}_activation_watch",
                 {"tapped": ao.get("tapped"), "stack": state.get("stack"),
                  "x": ST.get("x_answered"), "sac": ST.get("sac_submitted"),
                  "needs": ST["mana_needs"].get(tag), "vi": st.get("viewer_interaction")})
        diag_n += 1
        if ST.get("cost_seen_paid") and await ability_on_stack(state):
            say(f"[{tag}] ability on the stack; cost paid")
            ST["mana_needs"][tag] = {}
            return True
        if ST.get("cost_seen_paid") and not (state.get("stack") or []):
            # ability may have resolved very fast; let the caller check
            say(f"[{tag}] cost paid, stack empty -- proceeding")
            ST["mana_needs"][tag] = {}
            return True
        # fall through to the priority-pass gate (never return early from
        # the stack watch)
        if not real_decision_pending(st) and my_priority(acts):
            if PASSED_REV.get(p0.name, -1) < p0.revision:
                await pass_priority(p0, st, acts)
                PASSED_REV[p0.name] = p0.revision
    ST["mana_needs"][tag] = {}
    say(f"[{tag}] TIMEOUT in activate_artisan")
    wire(f"{tag}_activate_timeout", {})
    return False


async def tick_all_generic(p0, p1, g, tag):
    await p1_tick(p1, g, f"P1{tag}")
    st = p0.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(p0, acts, st, 0, tag):
        return
    if await do_bottom(p0, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(p0, acts, st, 0, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(p0, d)
        return
    if "DeclareBlockers" in atypes:
        return
    drain_rejections(p0, tag)
    # search prompt can also appear during generic driving (after sacrifice
    # resolves on a later tick)
    if await handle_search(p0, tag, st, state):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_tick(p0, acts):
            return
        if await pay_mana_vi(p0, st, tag, needs):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(p0.name, -1) < p0.revision:
            await pass_priority(p0, st, acts)
            PASSED_REV[p0.name] = p0.revision


async def settle(p0, p1, g, tag, cond, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await tick_all_generic(p0, p1, g, tag)
        await asyncio.sleep(0)
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle: {label}")
    wire("settle_timeout", {"label": label})
    return None


async def export_now(c, path):
    try:
        s = await c.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


# ---------------------------------------------------------------- main

async def main():
    await verify_server_hello()
    a1 = check_parse()

    tag = "7155"
    ST["mana_needs"][tag] = {}
    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck((ARTISAN.title(), 4), (ELVES.title(), 12),
                         (BEARS.title(), 4), (SWAMP, 20), (FOREST, 20)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((FOREST, 60)))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code})

    # ---- ramp to the activation window
    t_ramp = time.time()
    last_rev, last_change, last_tick_at, last_diag = {}, {}, {}, time.time()
    act = None
    try:
        while time.time() - t_ramp < 1200:
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
                    holds_prio = my_priority(top_acts(st))
                    if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        r = await p0_tick_ramp(c, {"mode": "main"}, tag)
                        if isinstance(r, tuple) and r[0] == "ACTIVATE":
                            act = r
                            break
                    else:
                        await p1_tick(c, {"mode": "main"}, f"P1{tag}")
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                    wire(f"{tag}_tick_error",
                         {"who": c.name, "err": f"{type(e).__name__}: {e}"})
            if act:
                break
            if time.time() - last_diag > 25:
                last_diag = time.time()
                for c in (p0, p1):
                    st = c.latest
                    if not st:
                        say(f"[{tag}] DIAG {c.name}: no state yet")
                        continue
                    s = st["state"]
                    acts = top_acts(st)
                    say(f"[{tag}] DIAG {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"act={s.get('active_player')} "
                        f"acts={[a.get('type') for a in acts][:8]} "
                        f"vikind={vi_kind_code(st)!r} "
                        f"real_decision={real_decision_pending(st)}")
                    wire(f"{tag}_diag", {"who": c.name, "rev": c.revision,
                                         "act_types": [a.get("type") for a in acts][:10]})
    except Exception as e:
        say(f"[{tag}] ramp loop exception: {e}")
    if not act or act[0] != "ACTIVATE":
        say(f"[{tag}] never reached activation window -- verdict blocked")
        await p0.close()
        await p1.close()
        return {"A1_parse_cost": a1, "A2_setup_ok": "not-run",
                "_notes": ["never reached activation window"],
                "_verdict": "blocked"}

    _, artisan_oid, action = act
    ST["_artisan_oid"] = artisan_oid
    say("exporting PRE_ACTIVATION state")
    pre_env_s = await export_now(p0, "pre_activation.json")
    pre_env = json.loads(pre_env_s)
    pre = pre_env["state"]
    # re-resolve the Artisan on the authoritative export
    art_ids = [oid for oid, o in (pre.get("objects") or {}).items()
               if o.get("zone") == "Battlefield"
               and str(o.get("controller", -1)) == "0"
               and str(o.get("base_name") or o.get("name") or "").lower() == ARTISAN]
    if art_ids:
        artisan_oid = art_ids[0]
    pre_untapped = len([o for oid, o in (pre.get("objects") or {}).items()
                        if o.get("zone") == "Battlefield"
                        and str(o.get("controller", -1)) == "0"
                        and "Land" in ((o.get("card_types") or {}).get("core_types") or [])
                        and not o.get("tapped")])
    say(f"ACTIVATING FIEND ARTISAN (id {artisan_oid}); "
        f"untapped lands={pre_untapped}")
    ok = await activate_artisan(p0, p1, {"mode": "main"}, tag, artisan_oid, action)
    await asyncio.sleep(0)

    decisive_x_fail = (not ok) and bool(ST.get("x_interaction_rejected"))
    if decisive_x_fail:
        say("decisive X-rejection captured; skipping search/settle loops")
        wire("skip_to_finalize", {"reason": "x_rejected_decisive"})
    else:
        # ---- drive to the search prompt (or capture the stuck state)
        def search_or_stuck(s):
            return ST.get("search_answered") or (
                ST.get("cost_seen_paid") and
                any(obj_lname(s, o) == BEARS for o in bf_oids(s, 0)))

        t1 = time.time()
        while time.time() - t1 < 240:
            await asyncio.sleep(0.5)
            await tick_all_generic(p0, p1, {"mode": "main"}, tag)
            await asyncio.sleep(0)
            st = p0.latest
            if st and search_or_stuck(st["state"]):
                break
        say("exporting MID_COST state")
        await export_now(p0, "mid_cost.json")

        # ---- settle to resolution or a stuck decision
        def resolved(s):
            return (ST.get("search_answered")
                    and any(obj_lname(s, o) == BEARS for o in bf_oids(s, 0))
                    and not (s.get("stack") or []))

        s3 = await settle(p0, p1, {"mode": "main"}, tag, resolved, 180,
                          "ability resolution")
    # record whether the X prompt is still pending at the end (stuck game)
    st_end = p0.latest
    still = False
    if st_end:
        for op in vi_ops(st_end):
            resp = op.get("response") or {}
            if resp.get("type") == "schema" and (
                    resp.get("data") or {}).get("spec", {}).get("type") == "number":
                still = True
                break
    ST["x_prompt_still_pending"] = still
    say(f"x_prompt_still_pending={still}")
    wire("x_prompt_still_pending", {"still": still})
    say("exporting POST state")
    post_env_s = await export_now(p0, "post.json")
    post = json.loads(post_env_s)["state"]

    await p0.close()
    await p1.close()
    return {"A1_parse_cost": a1, "_ok": ok, "_verdict": None,
            "_notes": [f"activation_ok={ok}"],
            "_rejections": list(ST["rejections"])}


def _load_env(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())
    except Exception:
        return None


def _env_state(env):
    if env is None:
        return None
    return env.get("state")


def _bf_named(state, pid, lname):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and str(o.get("base_name") or o.get("name") or "").lower() == lname]


def _untapped_lands(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and "Land" in ((o.get("card_types") or {}).get("core_types") or [])
            and not o.get("tapped")]


def _gy_named(state, pid, lname):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller", -1)) == str(pid)
            and str(o.get("base_name") or o.get("name") or "").lower() == lname]


def compute_assertions(a1, notes_extra):
    """Compute A1-A8 from the authoritative saved states + ST."""
    ass, notes = {}, list(notes_extra)
    ass["A1_parse_cost"] = a1

    pre = _env_state(_load_env("pre_activation.json"))
    mid = _env_state(_load_env("mid_cost.json"))
    post = _env_state(_load_env("post.json"))
    live = ST

    if pre is None:
        ass["A2_setup_ok"] = "not-run"
        notes.append("A2: pre_activation.json missing")
    else:
        arts = _bf_named(pre, 0, ARTISAN)
        ao = (pre.get("objects") or {}).get(str(arts[0])) if arts else None
        elves = _bf_named(pre, 0, ELVES)
        unt = _untapped_lands(pre, 0)
        unt_bg = [o for o in unt
                  if str((pre.get("objects") or {}).get(str(o), {})
                         .get("base_name") or "").lower() in ("swamp", "forest")]
        ok = bool(arts) and ao is not None and not ao.get("tapped") \
            and len(elves) >= 1 and len(unt) >= X_VALUE + 1 and len(unt_bg) >= 1
        ass["A2_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A2: artisan_bf={bool(arts)} tapped={ao.get('tapped') if ao else '?'} "
                     f"elves_bf={len(elves)} untapped_lands={len(unt)} "
                     f"(bg={len(unt_bg)}, need>={X_VALUE + 1})")

    ok3 = live.get("activation_offered", False)
    ass["A3_activation_offered"] = "passed" if ok3 else (
        "not-run" if ass["A2_setup_ok"] != "passed" else "failed")
    notes.append(f"A3: ActivateAbility offered={ok3} "
                 f"(attempts={live.get('activation_attempts', 0)})")

    ok4 = live.get("x_answered", False) and not live.get(
        "x_prompt_still_pending", False)
    ass["A4_x_prompt"] = "passed" if ok4 else (
        "not-run" if ass["A3_activation_offered"] != "passed" else "failed")
    notes.append(f"A4: X answered={live.get('x_answered', False)} "
                 f"(X={X_VALUE}); prompt still pending at end="
                 f"{live.get('x_prompt_still_pending', '?')}; "
                 f"interaction Number{{2}} rejected="
                 f"{live.get('x_interaction_rejected', False)}; "
                 f"legacy ChooseX{{2}} rejected="
                 f"{live.get('x_legacy_rejected', False)}")

    # A5: cost paid -- Artisan tapped + an Elves in the graveyard + the
    # {2}{B/G} mana consumed (untapped land count dropped by >= 3).
    ok5, det5 = False, "not-run"
    if pre is not None and (mid is not None or post is not None):
        later = post if post is not None else mid
        a_oid = live.get("_artisan_oid")
        arts_pre = _bf_named(pre, 0, ARTISAN)
        if a_oid is None and arts_pre:
            a_oid = arts_pre[0]
        live["_artisan_oid"] = a_oid
        ao_post = (later.get("objects") or {}).get(str(a_oid)) if a_oid else None
        elves_gy = _gy_named(later, 0, ELVES)
        du = len(_untapped_lands(pre, 0)) - len(_untapped_lands(later, 0))
        tapped = ao_post is not None and ao_post.get("tapped")
        ok5 = bool(tapped) and len(elves_gy) >= 1 and du >= X_VALUE + 1
        det5 = (f"artisan_tapped={bool(tapped)} elves_in_gy={len(elves_gy)} "
                f"untapped_delta={du} (need>={X_VALUE + 1})")
    ass["A5_cost_paid"] = "passed" if ok5 else (
        "failed" if det5 != "not-run" and ass["A4_x_prompt"] == "passed"
        else ("not-run" if det5 == "not-run" else "failed"))
    notes.append(f"A5: {det5}")

    ok6 = live.get("search_answered", False)
    ass["A6_search_prompt"] = "passed" if ok6 else (
        "not-run" if ass["A5_cost_paid"] != "passed" else "failed")
    notes.append(f"A6: library search offered+answered (Bears picked)={ok6} "
                 f"(search_prompted={live.get('search_prompted', False)})")

    ok7 = False
    if post is not None:
        bears = _bf_named(post, 0, BEARS)
        stack_empty = not (post.get("stack") or [])
        ok7 = bool(bears) and stack_empty
        notes.append(f"A7: bears_on_bf={bool(bears)} stack_empty={stack_empty}")
    else:
        ass["A7_resolution"] = "not-run"
        notes.append("A7: post.json missing")
    if "A7_resolution" not in ass:
        ass["A7_resolution"] = "passed" if ok7 else (
            "not-run" if ass["A6_search_prompt"] != "passed" else "failed")

    # A8: cleanup -- the game must be able to advance past the test. If the
    # X prompt is still pending at the end (engine rejecting every valid
    # answer), the game is stuck and cleanup fails.
    ok8 = False
    if post is not None:
        stack_empty = not (post.get("stack") or [])
        stuck = bool(live.get("x_prompt_still_pending", False))
        ok8 = stack_empty and not stuck
        notes.append(f"A8: stack_empty={stack_empty} "
                     f"x_prompt_still_pending={stuck}")
    ass["A8_cleanup"] = "passed" if ok8 else "failed"

    return ass, notes


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #7155 — Fiend Artisan: cannot pay for the ability (revalidation)",
           fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol "
                    f"{si['protocol_version']} — {run['run_id']}", fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120)}.get(
        run["verdict"], (230, 200, 90))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from authoritative saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse_cost": "A1 cost AST = Mana{X,B/G} + Tap + Sacrifice (Another Creature); search X-bound",
        "A2_setup_ok": "A2 setup: Artisan untapped on BF, >=1 Elves, >=3 untapped lands",
        "A3_activation_offered": "A3 ActivateAbility advertised on a main phase, empty stack",
        "A4_x_prompt": "A4 X prompt answered X=2, prompt advanced",
        "A5_cost_paid": "A5 {2}{B/G} paid + Artisan tapped + Elves sacrificed",
        "A6_search_prompt": "A6 library search offered; Grizzly Bears (MV 2<=2) picked",
        "A7_resolution": "A7 Bears entered P0 battlefield; ability left the stack",
        "A8_cleanup": "A8 stack empty, no stall, game advanced",
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
    for n in run["notes"][:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7155/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


async def amain():
    pidfile = "/tmp/scenario_7155_01030.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7155_01030 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    t0 = time.time()
    try:
        try:
            res = await asyncio.wait_for(main(), timeout=3000)
        except Exception as e:
            say(f"FATAL: {type(e).__name__}: {e}")
            wire("fatal", {"err": f"{type(e).__name__}: {e}"})
            res = {"A1_parse_cost": "not-run",
                   "_notes": [f"fatal: {type(e).__name__}: {e}"]}
        a1 = res.get("A1_parse_cost", "not-run")
        ass, notes = compute_assertions(a1, res.get("_notes", []))
        rejections = res.get("_rejections", [])
        if a1 != "passed":
            verdict = "blocked"
        elif ass.get("A2_setup_ok") != "passed":
            verdict = "blocked"
        elif any(ass.get(k) != "passed" for k in
                 ("A3_activation_offered", "A4_x_prompt", "A5_cost_paid",
                  "A6_search_prompt", "A7_resolution")):
            verdict = "reproduced"
        elif all(ass.get(k) == "passed" for k in ass):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
        notes.append(
            "protocol-106 driver: MulliganDecision via legacy Action; "
            "bottom/SelectCards + DiscardToHandSize via vi schema/select; "
            "casts use the engine's Auto payment (no driver-side taps for "
            "casts); ActivateAbility submitted holding priority; "
            "{X} answered from a pure numeric menu; {2}{B/G} driven via vi "
            "tapLandForMana choices only when offered (engine may auto-pay); "
            "sacrifice = Elves via vi object references; search = schema "
            "select picking Grizzly Bears; priority-gated passes; sleep(0) "
            "yield before leg evaluation; re-tick backstop; stack-watch "
            "falls through to the pass gate.")
        scenario_src = open(__file__, "rb").read()
        run = {
            "issue": ISSUE,
            "issue_url": "https://github.com/phase-rs/phase/issues/7155",
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
            "duration_s": round(time.time() - t0, 1),
            "server_identity": SERVER_IDENTITY,
            "server_run_dir": ("runs/20261008-7155a (run dir); reused the "
                               "already-listening v0.103.0 single-user server "
                               "on 127.0.0.1:9374, pid 4471 (started by run "
                               "20261008-7148; isolated games.db runs/20261008-7148)"),
            "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
            "scenario_sha256": sha256_of_bytes(scenario_src),
            "decks": {
                "P0": [["Fiend Artisan", 4], ["Llanowar Elves", 12],
                       ["Grizzly Bears", 4], ["Swamp", 20], ["Forest", 20]],
                "P1": [["Forest", 60]],
            },
            "assertions": ass,
            "notes": notes,
            "verdict": verdict,
            "result": "; ".join(f"{k}: {ass.get(k, 'not-run')}" for k in
                                ("A1_parse_cost", "A2_setup_ok",
                                 "A3_activation_offered", "A4_x_prompt",
                                 "A5_cost_paid", "A6_search_prompt",
                                 "A7_resolution", "A8_cleanup")),
            "scope": ("Fiend Artisan {X}{B/G},{T},Sacrifice-another-creature "
                      "activation path; native human seats (no AI); X=2, "
                      "sacrifice a Llanowar Elves, search for Grizzly Bears"),
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "States are authoritative exports, restorable only via full game "
                "replay (the phase-server has no standalone state-import path).",
            ],
            "setup_line": ("P0: 4x Fiend Artisan / 12x Llanowar Elves / 4x Grizzly Bears / "
                           "20x Swamp / 20x Forest; P1: 60x Forest (passive). P0 mulligans "
                           "for Artisan, casts Elves + Artisan (engine Auto payment)."),
            "contract_line": ("P0 activates the Artisan (X=2) on a main phase with an empty "
                              "stack: answers the X prompt, pays {2}{B/G}, taps, sacrifices "
                              "an Elves, answers the search with Grizzly Bears; the Bears "
                              "must enter the battlefield and the ability must resolve. "
                              "The report says the game does not let you pay for the ability."),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        with open(f"{EVDIR}/scenario_7155_01030.py", "w") as f:
            f.write(scenario_src.decode())
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": ass, "notes": notes,
                       "verdict": verdict,
                       "rejections": res.get("_rejections", [])}, f, indent=1)
        render_summary(run, f"{EVDIR}/summary.png")
        wire("verdict", {"verdict": verdict, "assertions": ass})
        say(f"VERDICT: {verdict}")
        WIRE.close()
        RUNLOG.close()
        lines = []
        for fn in sorted(os.listdir(EVDIR)):
            if fn == "manifest.sha256":
                continue
            lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read())
                         + "  " + fn)
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(amain())

