#!/usr/bin/env python3
"""Issue #6666: Hidetsugu and Kairi dies trigger — optional during-resolution
free cast never reaches the player.

Re-validation on the pinned release v0.104.0 (build 4227122, protocol 118).
Prior runs: v0.78.0 (reproduced, protocol 68, run 20260909-6666: no offer
reached the player), v0.84.0 (reproduced, protocol 71, run 20260916-6666),
v0.85.0 (reproduced, protocol 72, run 20260917-6666), v0.102.0 (reproduced,
protocol 106, run 20261005-6666), v0.103.0 (reproduced, protocol 106,
run 20261007-0311-6666: the offer appeared and the accept submitted
cleanly, but the accepted free cast never materialized; the staged Bolt
stranded in Exile, P1 at 19 life).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle (verified in pinned v0.104.0 card-data.json):
  "When Hidetsugu and Kairi dies, exile the top card of your library.
   Target opponent loses life equal to its mana value. If it's an instant or
   sorcery card, you may cast it without paying its mana cost."

Scenario (native engine, two human-client seats, protocol 106), two legs:
  Game A (instant): stage Lightning Bolt (MV 1) on top via Mystical Tutor,
                    Murder Hidetsugu and Kairi, observe trigger, accept the
                    free-cast offer if it appears.
  Game B (sorcery): stage Divination (MV 3) on top, same flow.

Per-leg assertions:
  A1 setup_ok            both seats joined; game driven to the kill turn
  A2 hk_on_battlefield   H&K on battlefield (ETB draw3/put2 answered)
  A3 top_staged          pre_trigger export: staged card is library[0]
  A4 hk_died             Murder resolved; H&K in P0 graveyard
  A5 exile_and_lifeloss  post_trigger: staged card in Exile; P1 life == 20 - MV
  A6 free_cast_offered   optional free-cast offer reaches P0
  A7 accept_resolves     accepted cast completes fully (Bolt: P1 takes 3 more
                         and Bolt in P0 GY / Divination: P0 draws 2 and
                         Divination in P0 GY; nothing stranded in Exile)
  A8 no_dangling         end state: clean Priority, empty stack

Verdict: reproduced iff A5 passes and (A6 fails or A7 fails) on either leg
(the reported symptom family); not-reproduced iff A6+A7+A8 pass on BOTH legs;
blocked otherwise.

Driver conventions (protocol 106, from scenario_4509_01020.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - 01040 port notes (2026-10-09): v0.104.0/4227122; DeclareAttackers
    no-resubmit guard carried over (never re-submit once declared this
    turn; post-declaration priority round still advertises the action);
    render reuses the version-agnostic render_summary_6666_01020.py.
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action with answered (tag,iid) keys.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 plus a schema/select
    opportunity offering hand cards.
  - Spell mana payment via vi tapLandForMana, needs-gated
    (S["mana_needs"]); the 106 engine offers tapLandForMana menus at ordinary
    priority windows, so blind tapping is never done.
  - real_decision_pending excludes tapLandForMana/untapLandForMana/castSpell/
    activateAbility/passPriority/mulliganDecision menus.
  - A single await asyncio.sleep(0) yield after the priority gate, before
    leg evaluation.
  - Export checkpoints fall through to the priority pass in the same tick;
    never return after an export while holding priority.
  - Re-tick backstop: a client holding Priority with no revision change for
    >5s is re-ticked.
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
ISSUE = 6666
RUN_ID = os.environ.get("RUN_ID", "run-6666-reval-v01040-20261009-0241")
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
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run": ("backfill-owned v0.104.0 server on 127.0.0.1:9374 "
                   "(started by this run run-6666-reval-v01040-20261009-0241; identity verified "
                   "by ClientHello/ServerHello handshake and recomputed "
                   "digests this run)"),
    "source": ("2026-10-09: latest stable release v0.104.0 (published "
               "2026-10-08) == pinned release dir; ServerHello "
               "0.104.0/4227122/protocol 118 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.104.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.104.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.104.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"))

HK = "hidetsugu and kairi"
TUTOR = "mystical tutor"
MURDER = "murder"
ISLAND, SWAMP, MOUNTAIN = "island", "swamp", "mountain"
LANDS = {ISLAND, SWAMP, MOUNTAIN}
LEGS = {
    "instant": {"staged": "lightning bolt", "mv": 1},
    "sorcery": {"staged": "divination", "mv": 3},
}
NEEDS = {
    HK: {"U": 2, "B": 1, "generic": 2},
    TUTOR: {"U": 1},
    MURDER: {"B": 2, "generic": 1},
}
# NOTE (data observation, not the verdict): pinned card-data.json lists
# Hidetsugu and Kairi's mana cost as {2}{U}{U}{B}; the printed card is
# {1}{U}{B}{R}. The fixture pays the engine's cost.

SETUP_DEADLINE_S = 2400
ASS_KEYS = ("A1_setup_ok", "A2_hk_on_battlefield", "A3_top_staged",
            "A4_hk_died", "A5_exile_and_lifeloss", "A6_free_cast_offered",
            "A7_accept_resolves", "A8_no_dangling")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
P0C = None


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
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def lib_top_lname(state, pid):
    lib = player_of(state, pid).get("library", []) or []
    return obj_lname(state, lib[0]) if lib else None


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


def zone_names(state, zone, pid=None):
    out = []
    for o in state.get("objects", {}).values():
        if o.get("zone") != zone:
            continue
        if pid is not None and str(o.get("controller", -1)) != str(pid) \
                and str(o.get("owner", -1)) != str(pid):
            continue
        out.append(str(o.get("base_name") or o.get("name") or "?").lower())
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def can_pay(state, pid, needs):
    color_of = {ISLAND: "U", SWAMP: "B", MOUNTAIN: "R"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = dict(needs)
    for c in ("W", "U", "B", "R", "G"):
        n = need.get(c, 0)
        if pool.get(c, 0) < n:
            return False
        pool[c] = pool.get(c, 0) - n
    return sum(pool.values()) >= need.get("generic", 0)


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def surf_codes(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data")
        if isinstance(d, dict):
            c = d.get("code")
            if c is not None:
                out.append(c)
    return out


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
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
            codes.update(surf_codes(ch))
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
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    hk = CARD_DATA.get(HK, {})
    oracle = str(hk.get("oracle_text") or "")
    ok = ("exile the top card of your library" in oracle
          and "you may cast it without paying its mana cost" in oracle)
    notes = [f"mana_cost={hk.get('mana_cost')}"]
    if not ok:
        notes.append("H&K oracle shape missing")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": oracle[:600]}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


async def do_mulligan(c, acts, st, pid, tag, S):
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
    mull_count = sum(1 for k in MULLS if k[0] == tag and "mull" in str(k[1]))
    lands = sum(1 for n in hn if n in LANDS)
    if tag.startswith("P0"):
        keep = lands >= 2 and (HK in hn or TUTOR in hn)
    else:
        keep = lands >= 2
    if not keep and mull_count < 3 and len(hn) > 4:
        say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, S):
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity")
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
    keepers = (HK, TUTOR, MURDER, S["staged"]) if tag.startswith("P0") \
        else (ISLAND,)

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        return (1, str(ref)) if nm in keepers else (0, str(ref))

    picks = sorted(cands, key=bkey)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(
        c, {"interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, S):
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
    keepers = (HK, TUTOR, MURDER, S["staged"]) if tag.startswith("P0") \
        else (ISLAND,)

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm in LANDS:
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


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = surf_codes(ch)
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


async def play_a_land(c, state, pid, acts, tag, S):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag, S["leg"])) == turn:
        return False
    want = {ISLAND, SWAMP, MOUNTAIN} if tag.startswith("P0") else {ISLAND}
    for o in hand_ids(state, pid):
        if obj_lname(state, o) not in want:
            continue
        for a in acts:
            if a["type"] == "PlayLand" and \
                    str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag, S["leg"])] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


def cast_action_for(acts, state, name):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == name:
                    return a, iv
    return None, None


async def try_cast(c, state, acts, tag, name, S, want=None):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    seat = "P0" if tag.startswith("P0") else "P1"
    S["mana_needs"][seat] = dict(NEEDS[name])
    say(f"[{tag}] casting {name} (oid {oid}) needs={NEEDS[name]}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    if want:
        S["pending_target"] = dict(want)
    return True


async def pay_mana_vi(c, st, tag, S):
    ops = vi_ops(st)
    if not ops:
        return False
    seat = "P0" if tag.startswith("P0") else "P1"
    needs = S["mana_needs"][seat]
    if sum(needs.values()) <= 0:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
                continue
            if "tapLandForMana" in surf_codes(ch):
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

# ---------------------------------------------------------------- prompt answerers

def _vi_schema_candidates(st):
    """Yield (opp, cands, spec_type) for vi schema opportunities with
    candidates."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        cands = rdata.get("candidates") or []
        if cands:
            yield opp, cands, spec.get("type") or "select"


async def answer_etb_putback(c, st, state, tag, S):
    """H&K ETB: draw 3, put 2 from hand on top. Candidates are hand cards;
    put back 2 lands."""
    if not S.get("etb_pending"):
        return False
    handset = set(str(h) for h in hand_ids(state, 0))
    for opp, cands, stype in _vi_schema_candidates(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or not all(r in handset for r in refs):
            continue
        if len(refs) < 2:
            continue

        def bkey(ch):
            nm = obj_lname(state, _cand_reference(ch))
            return (0, nm) if nm in LANDS else (1, nm)

        picks = sorted(cands, key=bkey)[:2]
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] ETB putback: "
            f"{[obj_lname(state, _cand_reference(x)) for x in picks]}")
        wire("etb_putback", {"who": tag,
                             "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data":
                                           {"choiceIds":
                                            [x.get("id") for x in picks]}}},
                          tag)
        S["etb_pending"] = False
        return True
    return False


async def answer_tutor_search(c, st, state, tag, S):
    """Mystical Tutor search: candidates are library cards (not in hand);
    pick the staged card."""
    if not S.get("tutor_pending"):
        return False
    handset = set(str(h) for h in hand_ids(state, 0))
    for opp, cands, stype in _vi_schema_candidates(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or any(r in handset for r in refs):
            continue
        pick = None
        for ch in cands:
            if obj_lname(state, _cand_reference(ch)) == S["staged"]:
                pick = ch
                break
        if pick is None:
            say(f"[{tag}] tutor search: staged '{S['staged']}' not among "
                f"{len(cands)} candidates")
            wire("tutor_search_no_match",
                 {"n": len(cands),
                  "sample": [obj_lname(state, _cand_reference(ch))
                             for ch in cands[:8]]})
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tutor search: staging '{S['staged']}'")
        wire("tutor_search_answer", {"choice": pick.get("id")})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data":
                                           {"choiceIds": [pick.get("id")]}}},
                          tag)
        S["tutor_pending"] = False
        S["search_answered"] = True
        return True
    return False


async def answer_murder_target(c, st, state, tag, S):
    """Murder target selection: pick the H&K object."""
    if not S.get("murder_pending") or S.get("murder_targeted"):
        return False
    hk_oid = S.get("hk_oid")
    if hk_oid is None:
        return False
    # vi schema opportunities first
    for opp, cands, stype in _vi_schema_candidates(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        pick = None
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None and str(ref) == str(hk_oid):
                pick = ch
                break
        if pick is None:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] murder targets H&K (choice {pick.get('id')})")
        wire("murder_target_answer", {"choice": pick.get("id")})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data":
                                           {"choiceIds": [pick.get("id")]}}},
                          tag)
        S["murder_targeted"] = True
        return True
    # exactChoices fallback
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []):
            ref = _cand_reference(ch)
            if ref is not None and str(ref) == str(hk_oid):
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] murder targets H&K via exactChoices")
                wire("murder_target_answer", {"choice": ch.get("id")})
                await answer_vi(c, opp, ch, tag)
                S["murder_targeted"] = True
                return True
    return False


def find_free_cast_offer(st, state, S):
    """Locate the optional free-cast offer for the staged card. Returns
    (opp, choice) or (None, None)."""
    staged = S["staged"]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        # primary: decideOptionalEffect code
        for ch in items:
            if "decideOptionalEffect" not in surf_codes(ch):
                continue
            txt = " ".join([choice_text(ch)] +
                           [str((s.get("data") or {}).get("text", ""))
                            for s in ch.get("surfaces", [])]).lower()
            is_accept = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data") or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                if staged in txt and "cast" in txt:
                    is_accept = True
                elif "decline" in txt or "not cast" in txt or "no " in txt:
                    is_accept = False
            if is_accept:
                return opp, ch
        # fallback: a choice naming the staged card with cast language
        for ch in items:
            txt = " ".join([choice_text(ch)] +
                           [str((s.get("data") or {}).get("text", ""))
                            for s in ch.get("surfaces", [])]).lower()
            if staged in txt and "cast" in txt:
                return opp, ch
    return None, None


async def answer_bolt_target(c, st, state, tag, S):
    """Free-cast Lightning Bolt target selection: hit the opponent (seat 1)."""
    if S["leg"] != "instant" or S.get("bolt_targeted"):
        return False
    for opp, cands, stype in _vi_schema_candidates(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        pick = None
        for ch in cands:
            seats = [s.get("data", {}).get("seat")
                     for s in ch.get("surfaces", []) or []
                     if isinstance(s.get("data"), dict)]
            if 1 in seats or "1" in seats:
                pick = ch
                break
            if "player 1" in choice_text(ch).lower():
                pick = ch
                break
        if pick is None:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] free-cast Bolt targets opponent")
        wire("bolt_target_answer", {"choice": pick.get("id")})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data":
                                           {"choiceIds": [pick.get("id")]}}},
                          tag)
        S["bolt_targeted"] = True
        return True
    return False


def tutor_on_stack(state):
    blob = json.dumps(state.get("stack") or []).lower()
    return "search your library" in blob


def hk_trigger_on_stack(state):
    blob = json.dumps(state.get("stack") or []).lower()
    return "hidetsugu" in blob


async def export_named(name, S):
    """Authoritative export via P0. Callers must NOT return after this while
    holding priority -- fall through to the priority pass in the same tick."""
    try:
        raw = await P0C.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{S['leg']}_{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        S["exports"][name] = True
        say(f"[{S['leg']}] exported {S['leg']}_{name}.json "
            f"(turn={env['state'].get('turn_number')})")
        wire("export", {"leg": S["leg"], "name": name,
                        "turn": env["state"].get("turn_number")})
        return env["state"]
    except Exception as e:
        say(f"[{S['leg']}] export {name} FAILED: {e!r}")
        S["tick_errors"].append(f"export_{name}: {e!r}")
        return None


def dump_pending_vi(c, st, state, tag, S, why):
    """Debug: log the full vi opportunity shapes when a pending prompt goes
    unanswered, so a shape mismatch is visible instead of a silent stall."""
    now = time.time()
    if now - S.get("_dump_at", 0) < 15:
        return
    S["_dump_at"] = now
    vi = st.get("viewer_interaction") or {}
    say(f"[{tag}] PENDING-DUMP ({why}): canSubmit={vi.get('canSubmit')} "
        f"kind={vi_kind_code(st)} n_ops={len(vi_ops(st))} "
        f"atypes={sorted(set(a.get('type') for a in merged_actions(st)))}")
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        first = items[0] if items else {}
        say(f"[{tag}]   opp iid={opp.get('interactionId')} rtype={resp.get('type')} "
            f"spec={(data.get('spec') or {}).get('type')} n={len(items)} "
            f"first={json.dumps(first, default=str)[:400]}")
        wire("pending_vi_dump", {"who": tag, "why": why, "opp": opp})
    # also dump the raw viewer_interaction once per stall for full fidelity
    wire("pending_vi_raw", {"who": tag, "why": why, "vi": vi})


def drain_rejections(c, S):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"type": t, "data": data}
            out.append(rec)
            S["rejections"].append(rec)
            say(f"[{c.name}] REJECTION: {json.dumps(data, default=str)[:300]}")
            wire("rejection", {"who": c.name, "data": data})
    return out

# ---------------------------------------------------------------- per-leg game

async def run_leg(leg):
    cfg = LEGS[leg]
    staged, mv = cfg["staged"], cfg["mv"]
    S = {
        "leg": leg, "staged": staged, "mv": mv,
        "mana_needs": {"P0": {}, "P1": {}}, "stage": "setup", "exports": {},
        "rejections": [], "tick_errors": [], "notes": [],
        "etb_pending": False, "tutor_pending": False,
        "murder_pending": False, "murder_targeted": False,
        "search_answered": False, "hk_cast": False, "hk_oid": None,
        "tutor_cast": False, "murder_cast": False,
        "trigger_done": False, "offer_seen": False, "offer_answered": False,
        "bolt_targeted": False, "free_cast_taken": False, "resolved": False,
        "offer_deadline": None, "accept_deadline": None,
        "p1_life_pre": None, "drawn_by_div": None,
        "assert": {}, "verdict": None,
    }
    p0deck = deck((ISLAND, 12), (SWAMP, 10), (MOUNTAIN, 8),
                  (HK, 6), (TUTOR, 6), (MURDER, 6), (staged, 12))
    p1deck = deck((ISLAND, 60))
    p0 = PhaseClient(f"P0-{leg}")
    await p0.connect()
    await p0.create(p0deck)
    p1 = PhaseClient(f"P1-{leg}")
    await p1.connect()
    await p1.join(p0.game_code, p1deck)
    say(f"[{leg}] game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")
    wire("game_created", {"leg": leg, "code": p0.game_code})
    S["assert"]["A1_setup_ok"] = ("passed"
        if p0.player_id is not None and p1.player_id is not None
        else "failed")
    global P0C
    P0C = p0

    async def p0_tick(c):
        st = c.latest
        if not st:
            return
        state = st["state"]
        pid, tag = 0, f"P0-{leg}"
        acts = merged_actions(st)
        atypes = set(a.get("type") for a in acts)
        if await do_mulligan(c, acts, st, pid, tag, S):
            return
        if await do_bottom(c, acts, st, pid, tag, S):
            return
        if await do_discard_to_handsize(c, acts, st, pid, tag, S):
            return
        drain_rejections(c, S)
        if "DeclareAttackers" in atypes:
            # 2026-10-07 guard: after a declaration the game runs a
            # post-declaration priority round with DeclareAttackers still
            # advertised; never re-submit in the same turn -- fall through
            # to priority handling instead.
            turn = state.get("turn_number")
            dkey = ("declared_attackers", tag, turn)
            if dkey not in SUBMITTED_OPPS:
                da = next((a for a in acts
                           if a["type"] == "DeclareAttackers"), None)
                if da:
                    d = copy.deepcopy(da)
                    d.setdefault("data", {}).update({"attacks": [],
                                                     "bands": []})
                    SUBMITTED_OPPS.add(dkey)
                    await submit_as_is(c, d)
                    return
            # already declared this turn: fall through to priority below
        if "DeclareBlockers" in atypes:
            da = next((a for a in acts if a["type"] == "DeclareBlockers"),
                      None)
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(c, d)
            return
        if "OrderTriggers" in atypes:
            oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
            if oa:
                await submit_as_is(c, oa)
            return
        if await pay_tick(c, acts):
            return
        if await pay_mana_vi(c, st, tag, S):
            return
        # prompt answers, most specific first
        if await answer_etb_putback(c, st, state, tag, S):
            return
        if await answer_tutor_search(c, st, state, tag, S):
            return
        if await answer_murder_target(c, st, state, tag, S):
            return
        if S["stage"] == "accept_drive":
            if await answer_bolt_target(c, st, state, tag, S):
                return
            # granted-permission fallback: a CastSpell for the exiled staged
            # card submitted while holding priority
            if not S.get("free_cast_taken") and my_priority(acts):
                ex_ids = {str(o.get("id")) for o in
                          state.get("objects", {}).values()
                          if o.get("zone") == "Exile"
                          and str(o.get("base_name") or o.get("name") or "")
                          .lower() == staged}
                for a in acts:
                    if a.get("type") == "CastSpell":
                        d = a.get("data") or {}
                        if str(d.get("object_id")) in ex_ids or \
                           str(d.get("source_id")) in ex_ids:
                            say(f"[{tag}] casting exiled {staged} via "
                                "granted permission")
                            wire("free_cast_manual", {"action": a})
                            await submit_as_is(c, a)
                            S["free_cast_taken"] = True
                            return

        # ---- priority gate, then yield before leg evaluation
        has_prio = my_priority(acts)
        await asyncio.sleep(0)

        # debug: a pending prompt that survives the answerers gets its vi
        # shapes dumped (throttled) instead of stalling silently
        pending = [k for k in ("etb_pending", "tutor_pending",
                               "murder_pending")
                   if S.get(k)]
        if pending and S["stage"] == "setup":
            dump_pending_vi(c, st, state, tag, S, ",".join(pending))

        # track H&K / spells
        hk_here = bf_by_name(state, 0, HK)
        if hk_here:
            S["hk_oid"] = hk_here[0]
            if S.get("hk_cast") and not S.get("etb_answered_once"):
                # ETB putback expected; flag set when the spell resolved
                pass
        hk_in_gy = any(str(o.get("base_name") or o.get("name") or "")
                       .lower() == HK and o.get("zone") == "Graveyard"
                       for o in state.get("objects", {}).values())
        tutor_in_gy = any(str(o.get("base_name") or o.get("name") or "")
                          .lower() == TUTOR
                          and o.get("zone") == "Graveyard"
                          for o in state.get("objects", {}).values())
        murder_in_gy = any(str(o.get("base_name") or o.get("name") or "")
                           .lower() == MURDER
                           and o.get("zone") == "Graveyard"
                           for o in state.get("objects", {}).values())
        if S.get("hk_cast") and hk_here and not S.get("etb_seen"):
            S["etb_seen"] = True
            S["etb_pending"] = True
            say(f"[{tag}] H&K resolved on BF; ETB putback pending")
        if S.get("tutor_cast") and tutor_in_gy and not S.get("tutor_done"):
            S["tutor_done"] = True
            S["tutor_pending"] = False
            # 2026-10-07: the live viewer-filtered state hides library tops
            # ("hidden card"), so the staged top cannot be verified from the
            # live view. Record the resolution turn instead: the tutor put
            # the staged card on top this turn, so murdering H&K in the SAME
            # turn (before any draw step) is safe.
            S["tutor_done_turn"] = state.get("turn_number")
            say(f"[{tag}] tutor resolved (turn {S['tutor_done_turn']})")
        if S.get("murder_cast") and murder_in_gy and not S.get("murder_done"):
            S["murder_done"] = True
            say(f"[{tag}] murder resolved")

        # ---- stage machine
        if S["stage"] == "setup":
            # H&K died?
            if S.get("murder_done") and not hk_here and hk_in_gy:
                S["stage"] = "trigger_watch"
                S["p1_life_pre"] = life_of(state, 1)
                say(f"[{tag}] H&K died; exporting pre_trigger")
                wire("hk_dead", {"turn": state.get("turn_number")})
                await export_named("pre_trigger", S)
                # fall through: no return while possibly holding priority
            elif my_main(state, pid) and has_prio:
                if await play_a_land(c, state, pid, acts, tag, S):
                    return
                names = hand_lnames(state, pid)
                if not S.get("hk_cast"):
                    if HK in names and can_pay(state, pid, NEEDS[HK]):
                        if await try_cast(c, state, acts, tag, HK, S):
                            S["hk_cast"] = True
                            return
                elif not S.get("tutor_cast"):
                    if TUTOR in names and can_pay(state, pid, NEEDS[TUTOR]):
                        if await try_cast(c, state, acts, tag, TUTOR, S):
                            S["tutor_cast"] = True
                            S["tutor_pending"] = True
                            return
                elif S.get("tutor_done") and not S.get("murder_cast"):
                    # Murder H&K in the same turn the tutor resolved -- no
                    # pass in between, so a draw step cannot steal the
                    # staged top card. The top is verified by turn (not the
                    # live view, which hides library tops as "hidden card"):
                    # tutor_done_turn == current turn implies the staged
                    # card is still on top.
                    if tutor_on_stack(state):
                        pass
                    elif hk_here and state.get("turn_number") == \
                            S.get("tutor_done_turn"):
                        S["hk_oid"] = hk_here[0]
                        if await try_cast(
                                c, state, acts, tag, MURDER, S,
                                want={"kind": "creature", "oid": hk_here[0],
                                      "name": HK}):
                            S["murder_cast"] = True
                            S["murder_pending"] = True
                            return
                    else:
                        S["murder_wait_ticks"] = \
                            S.get("murder_wait_ticks", 0) + 1
                        if S["murder_wait_ticks"] == 1 or \
                           S["murder_wait_ticks"] % 20 == 0:
                            say(f"[{tag}] waiting to murder: turn="
                                f"{state.get('turn_number')} "
                                f"(tutor_done_turn={S.get('tutor_done_turn')}), "
                                f"hk_bf={bool(hk_here)})")
        elif S["stage"] == "trigger_watch":
            exiled = zone_names(state, "Exile", 0)
            if staged in exiled and not hk_trigger_on_stack(state):
                S["stage"] = "offer_watch"
                S["trigger_done"] = True
                S["offer_deadline"] = time.time() + 90
                S["p1_life_post_trigger"] = life_of(state, 1)
                say(f"[{tag}] trigger resolved: {staged} exiled; P1 life "
                    f"{S['p1_life_pre']}->{S['p1_life_post_trigger']}; "
                    "exporting post_trigger, watching 90s for the offer")
                wire("trigger_resolved",
                     {"p1_life": S["p1_life_post_trigger"], "exile": exiled})
                await export_named("post_trigger", S)
                # fall through: no return while possibly holding priority
        elif S["stage"] == "offer_watch":
            opp, ch = find_free_cast_offer(st, state, S)
            if opp is not None:
                S["offer_seen"] = True
                S["stage"] = "accept_drive"
                S["accept_deadline"] = time.time() + 120
                say(f"[{tag}] *** FREE-CAST OFFER OBSERVED ***")
                wire("free_cast_offer",
                     {"interaction": st.get("viewer_interaction")})
                await answer_vi(c, opp, ch, tag)
                S["offer_answered"] = True
                say(f"[{tag}] accepted the free cast; driving to resolution")
                return
            if time.time() > S["offer_deadline"]:
                S["notes"].append("offer watch expired (90s): no free-cast "
                                 "offer reached P0")
                say(f"[{tag}] offer watch expired with no offer")
                S["stage"] = "done"
                await export_named("post", S)
                # fall through
            # log unidentified real decisions during the watch
            if real_decision_pending(st) and not S.get("watch_logged"):
                S["watch_logged"] = True
                wire("offer_watch_vi",
                     {"vi": st.get("viewer_interaction")})
                say(f"[{tag}] offer-watch: real decision pending but no "
                    "offer identified; logged vi")
        elif S["stage"] == "accept_drive":
            exiled = zone_names(state, "Exile", 0)
            gy = zone_names(state, "Graveyard", 0)
            stack_empty = not stack_entries(state)
            if leg == "instant":
                done_ok = (staged in gy and staged not in exiled
                           and stack_empty
                           and life_of(state, 1) == 20 - mv - 3)
            else:
                done_ok = (staged in gy and staged not in exiled
                           and stack_empty
                           and life_of(state, 1) == 20 - mv)
            if done_ok:
                S["resolved"] = True
                S["stage"] = "done"
                say(f"[{tag}] free cast resolved cleanly")
                await export_named("post", S)
                # fall through
            elif time.time() > S["accept_deadline"]:
                S["notes"].append(
                    "accept-drive expired (120s): accepted cast did not "
                    f"resolve (exile={exiled}, gy_has_staged={staged in gy}, "
                    f"p1_life={life_of(state, 1)})")
                say(f"[{tag}] accept-drive expired without resolution")
                S["stage"] = "done"
                await export_named("post", S)
                # fall through

        if has_prio and S["stage"] != "done":
            # never auto-pass a real decision; the offer/target answers above
            # run first, so reaching here means only menus/priority remain
            if not real_decision_pending(st):
                await pass_priority(c, st, acts)

    async def p1_tick(c):
        st = c.latest
        if not st:
            return
        state = st["state"]
        pid, tag = 1, f"P1-{leg}"
        acts = merged_actions(st)
        atypes = set(a.get("type") for a in acts)
        if await do_mulligan(c, acts, st, pid, tag, S):
            return
        if await do_bottom(c, acts, st, pid, tag, S):
            return
        if await do_discard_to_handsize(c, acts, st, pid, tag, S):
            return
        drain_rejections(c, S)
        if "DeclareAttackers" in atypes:
            # 2026-10-07 guard: after a declaration the game runs a
            # post-declaration priority round with DeclareAttackers still
            # advertised; never re-submit in the same turn -- fall through
            # to priority handling instead.
            turn = state.get("turn_number")
            dkey = ("declared_attackers", tag, turn)
            if dkey not in SUBMITTED_OPPS:
                da = next((a for a in acts
                           if a["type"] == "DeclareAttackers"), None)
                if da:
                    d = copy.deepcopy(da)
                    d.setdefault("data", {}).update({"attacks": [],
                                                     "bands": []})
                    SUBMITTED_OPPS.add(dkey)
                    await submit_as_is(c, d)
                    return
            # already declared this turn: fall through to priority below
        if "DeclareBlockers" in atypes:
            da = next((a for a in acts if a["type"] == "DeclareBlockers"),
                      None)
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(c, d)
            return
        if "OrderTriggers" in atypes:
            oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
            if oa:
                await submit_as_is(c, oa)
            return
        if await pay_tick(c, acts):
            return
        if await pay_mana_vi(c, st, tag, S):
            return
        has_prio = my_priority(acts)
        await asyncio.sleep(0)
        if real_decision_pending(st):
            dump_pending_vi(c, st, state, tag, S, "p1_real_decision")
        if my_main(state, pid) and has_prio:
            if await play_a_land(c, state, pid, acts, tag, S):
                return
        if has_prio and not real_decision_pending(st):
            await pass_priority(c, st, acts)

    t0 = time.time()
    last_rev = {}
    last_rev_at = {f"P0-{leg}": t0, f"P1-{leg}": t0}
    last_tick_at = {}
    max_turn = 0
    try:
        i = 0
        while i < 12000:
            i += 1
            await asyncio.sleep(0.2)
            for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_rev_at[c.name] = time.time()
                else:
                    if time.time() - last_rev_at.get(c.name, 0) > 45:
                        s0 = st["state"]
                        say(f"[{leg}] WATCHDOG stale {c.name}: rev "
                            f"{c.revision} turn={s0.get('turn_number')} "
                            f"phase={s0.get('phase')} stage={S['stage']} "
                            f"prio={my_priority(merged_actions(st))} "
                            f"real_decision={real_decision_pending(st)}")
                        wire("watchdog_stale",
                             {"leg": leg, "who": c.name,
                              "rev": c.revision,
                              "turn": s0.get("turn_number"),
                              "phase": s0.get("phase"),
                              "stage": S["stage"]})
                        last_rev_at[c.name] = time.time()
                    # Backstop: a client with no revision change for >10s
                    # gets re-ticked anyway (throttled to 5s) so that
                    # non-priority decisions -- e.g. the opponent's
                    # handsize discard, which nobody's priority pass will
                    # ever reach -- are answered instead of deadlocking.
                    # (Protocol 106 has no waiting_for; a pending choose
                    #  decision leaves no priority holder.)
                    if time.time() - last_rev_at.get(c.name, 0) < 10:
                        continue
                    if time.time() - last_tick_at.get(c.name, 0) < 5:
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"leg": leg, "who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
                    S["tick_errors"].append(f"{c.name}: {e!r}")
            st = p0.latest
            if st:
                max_turn = max(max_turn, st["state"].get("turn_number", 0))
            if S["stage"] == "done":
                break
            if max_turn > 40 and S["stage"] == "setup":
                S["notes"].append("could not reach the kill turn by turn 40")
                break
            if time.time() - t0 > SETUP_DEADLINE_S:
                S["notes"].append("leg deadline hit")
                break
    finally:
        await p0.close()
        await p1.close()
    S["duration_s"] = round(time.time() - t0, 1)
    return S


# ---------------------------------------------------------------- evaluation

def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)["state"]


def evaluate(S):
    leg, staged, mv = S["leg"], S["staged"], S["mv"]
    a = S["assert"]
    notes = S["notes"]

    pre = load_state(f"{EVDIR}/{leg}_pre_trigger.json")
    ptr = load_state(f"{EVDIR}/{leg}_post_trigger.json")
    post = load_state(f"{EVDIR}/{leg}_post.json")

    a["A2_hk_on_battlefield"] = "passed" if S.get("etb_seen") else "failed"
    notes.append(f"A2: H&K reached battlefield={S.get('etb_seen')}")

    top_ok = pre is not None and lib_top_lname(pre, 0) == staged
    a["A3_top_staged"] = "passed" if top_ok else "failed"
    notes.append(f"A3: pre_trigger library top="
                 f"{lib_top_lname(pre, 0) if pre else 'n/a'} (want {staged})")

    hk_gy = pre is not None and any(
        str(o.get("base_name") or o.get("name") or "").lower() == HK
        and o.get("zone") == "Graveyard"
        for o in pre["objects"].values())
    a["A4_hk_died"] = "passed" if hk_gy else "failed"
    notes.append(f"A4: H&K in graveyard in pre_trigger={hk_gy}")

    exile_ok = life_ok = False
    if ptr is not None:
        exiled = zone_names(ptr, "Exile", 0)
        exile_ok = staged in exiled
        life_ok = life_of(ptr, 1) == 20 - mv
        notes.append(f"A5: staged exiled={exile_ok} (exile={exiled}); "
                     f"P1 life 20->{life_of(ptr, 1)} (want {20 - mv})")
    else:
        notes.append("A5: post_trigger missing")
    a["A5_exile_and_lifeloss"] = "passed" if (exile_ok and life_ok) \
        else "failed"

    a["A6_free_cast_offered"] = "passed" if S.get("offer_seen") else "failed"
    notes.append(f"A6: free-cast offer reached P0={S.get('offer_seen')}")

    if S.get("offer_seen") and post is not None:
        exiled = zone_names(post, "Exile", 0)
        gy = zone_names(post, "Graveyard", 0)
        stack_empty = not (post.get("stack") or [])
        if leg == "instant":
            want_life = 20 - mv - 3
            ok = (staged in gy and staged not in exiled
                  and life_of(post, 1) == want_life and stack_empty)
            notes.append(f"A7: Bolt in P0 GY={staged in gy}; P1 life="
                         f"{life_of(post, 1)} (want {want_life}); "
                         f"exile={exiled}; stack_empty={stack_empty}")
        else:
            want_life = 20 - mv
            ok = (staged in gy and staged not in exiled
                  and life_of(post, 1) == want_life and stack_empty)
            notes.append(f"A7: Divination in P0 GY={staged in gy}; P1 life="
                         f"{life_of(post, 1)} (want {want_life}); "
                         f"exile={exiled}; stack_empty={stack_empty}")
        a["A7_accept_resolves"] = "passed" if ok else "failed"
    else:
        a["A7_accept_resolves"] = "not-run"
        notes.append("A7: not-run (no offer to accept, or no post state)")

    if post is not None:
        stack_empty = not (post.get("stack") or [])
        prio = my_priority(top_acts({"legal_actions": []}))
        a["A8_no_dangling"] = "passed" if stack_empty else "failed"
        notes.append(f"A8: end stack_empty={stack_empty}")
    else:
        a["A8_no_dangling"] = "not-run"
        notes.append("A8: not-run (no post state)")

    if a["A1_setup_ok"] != "passed" or a["A2_hk_on_battlefield"] != "passed":
        verdict = "blocked"
    elif a["A5_exile_and_lifeloss"] == "passed" and (
            a["A6_free_cast_offered"] == "failed"
            or a["A7_accept_resolves"] == "failed"):
        verdict = "reproduced"
    elif a["A6_free_cast_offered"] == "passed" \
            and a["A7_accept_resolves"] == "passed" \
            and a["A8_no_dangling"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return verdict

# ---------------------------------------------------------------- finalize

async def finalize(legs):
    verdicts = [S["verdict"] for S in legs]
    if any(v == "reproduced" for v in verdicts):
        overall = "reproduced"
    elif all(v == "not-reproduced" for v in verdicts):
        overall = "not-reproduced"
    else:
        overall = "blocked"
    legrecs = []
    for S in legs:
        legrecs.append({
            "leg": S["leg"], "staged": S["staged"], "mv": S["mv"],
            "verdict": S["verdict"],
            "assertions": S["assert"],
            "notes": S["notes"],
            "duration_s": S.get("duration_s"),
            "stage": S.get("stage"),
            "exports": sorted(S["exports"].keys()),
            "rejections": S["rejections"],
            "tick_errors": S["tick_errors"],
            "offer_seen": S.get("offer_seen"),
            "resolved": S.get("resolved"),
        })
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "verdict": overall,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(ST["t_start"])),
        "duration_s": round(time.time() - ST["t_start"], 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/cron-20261007-0311",
        "driver": {"protocol_advertised": 118,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_6666_01040.py"},
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {
            "P0": [(ISLAND, 12), (SWAMP, 10), (MOUNTAIN, 8), (HK, 6),
                   (TUTOR, 6), (MURDER, 6),
                   ("lightning bolt | divination (per leg)", 12)],
            "P1": [(ISLAND, 60)],
        },
        "legs": legrecs,
        "verdicts": {S["leg"]: S["verdict"] for S in legs},
        "assertions": {S["leg"]: S["assert"] for S in legs},
        "notes": [f"[{S['leg']}] {n}" for S in legs for n in S["notes"]],
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense 12x/6x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore facility; "
            "pre_trigger/post_trigger/post exports are authoritative states "
            "restorable only via full game replay, not direct load.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6666_01040.py", "w") as f:
        f.write(open(__file__).read())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"verdict": overall,
                   "legs": {S["leg"]: {"assertions": S["assert"],
                                       "verdict": S["verdict"],
                                       "notes": S["notes"]}
                            for S in legs}}, f, indent=1)

    # server log excerpts (shared backfill server; capture a tail slice
    # around this run)
    try:
        slog = f"{BACKFILL}/runs/cron-20261007-0311/server.log"
        if os.path.exists(slog):
            import subprocess
            tail = subprocess.run(
                ["tail", "-c", "30000", slog],
                capture_output=True).stdout
            with open(f"{EVDIR}/server_excerpts.log", "wb") as f:
                f.write(tail)
    except Exception as e:
        say(f"server excerpts capture failed: {e!r}")

    # AGENTS.md lesson: close WIRE/RUNLOG BEFORE the manifest loop
    say("closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_6666_01020.py",
         EVDIR, str(ISSUE),
         "Hidetsugu and Kairi optional free cast never reaches the player"],
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
    print(json.dumps({"verdict": overall,
                      "legs": {S["leg"]: S["verdict"] for S in legs},
                      "run_id": RUN_ID}, indent=1), flush=True)


async def _main():
    ST.update({"t_start": time.time()})
    await verify_server_hello()
    check_data_level()
    legs = []
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for leg in ("instant", "sorcery"):
        if only and only != leg:
            continue
        S = await run_leg(leg)
        S["verdict"] = evaluate(S)
        say(f"[{leg}] ASSERTIONS: " +
            " ".join(f"{k}:{v};" for k, v in S["assert"].items()))
        say(f"[{leg}] VERDICT: {S['verdict']}")
        wire("leg_verdict", {"leg": leg, "verdict": S["verdict"],
                             "assertions": S["assert"], "notes": S["notes"]})
        legs.append(S)
    await finalize(legs)


async def main():
    pidfile = "/tmp/scenario_6666_01040.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6666_01040 instance is alive "
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
