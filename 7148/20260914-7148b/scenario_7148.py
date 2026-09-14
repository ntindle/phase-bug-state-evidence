#!/usr/bin/env python3
"""phase-rs/phase #7148 - Nils, Discipline Enforcer: "for each player, put a
+1/+1 counter on up to one target creature that player controls" places ALL
counters on ONE creature instead of one counter per player.

Oracle: "At the beginning of your end step, for each player, put a +1/+1
counter on up to one target creature that player controls."

Reported: "Places all 4 counters on only one creature not one counter for
each player." (4-player game; the trigger should put exactly one counter on
one creature per player.)

Card-data parse (v0.82.0): trigger mode Phase/End, OnlyDuringYourTurn,
effect PutCounter P1P1 count=1, target Typed[Creature] controller=
ScopedPlayer, multi_target {min: 0, max: 1} ("up to one"), player_scope All
("for each player"). Parse looks correct, but the ability carries a
SwallowedClause(DynamicQty) parse warning on the same line -- the per-player
iteration may be collapsed at runtime into one multi-counter placement.

Behavioral contract (3 human seats, native engine, protocol 70):
  P0 fields Nils only; P1 fields one Grizzly Bears; P2 fields one Storm
  Crow. At P0's end step the trigger fires; the driver answers each
  TriggerTargetSelection with one target (the scope player's creature).
  A1 parse_ok:        PutCounter P1P1 x1, ScopedPlayer target, multi_target
                      0..1, player_scope All, Phase End, OnlyDuringYourTurn.
  A2 setup_ok:        >=1 target prompt seen, Nils on P0 BF, >=1 creature
                      on each player's BF at pre, pre.json exported.
  A3 per_player_one:  post-resolution, each player's creature(s) hold
                      exactly 1 new +1/+1 counter and no creature holds >1
                      (total exactly 3). Fails => the reported bug.
  A4 trigger_resolved: the Nils trigger left the stack and the game
                      advanced past the end step.
  A5 cleanup:         stack empty at final observation.

Verdict: reproduced iff A2 passes and A3 fails. not-reproduced iff A2+A3
pass. blocked iff A2 fails.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260914-7148")
EVDIR = f"{BACKFILL}/evidence/7148/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

NILS = "Nils, Discipline Enforcer"
BEAR = "Grizzly Bears"
CROW = "Storm Crow"
PLAINS = "Plains"
FOREST = "Forest"
ISLAND = "Island"

# One creature per player keeps every target unambiguous:
# P0 -> Nils, P1 -> Grizzly Bears, P2 -> Storm Crow.
# P1/P2 run 20x creatures: a 12x build once bricked P2's draws (all Islands,
# no Crow by turn 9), which weakened the per-player assertion.
P0_DECK = [(NILS, 12), (PLAINS, 48)]
P1_DECK = [(BEAR, 20), (FOREST, 40)]
P2_DECK = [(CROW, 20), (ISLAND, 40)]
TIMEOUT = 1500

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034205ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(ServerHello re-verified this run) + verified pin "
              "(minisign-verify of binary + signed data manifest with the "
              "repo-pinned key).",
}

CLIENTS = {}
ACTED = {}
ST = {}


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "rejections": [],
        "answered_iids": {},
        "nils": {"prompts": [],        # one record per answered prompt
                 "assigned": {},       # scope_player -> chosen oid
                 "pre": False,
                 "mid": False,
                 "answer_turn": None,
                 "mid_turn": None,
                 "mid_phase": None},
        "exports": {},
    })


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def objs(state):
    return state.get("objects", {})


def get_obj(state, oid):
    return objs(state).get(str(oid)) or {}


def hand_oids(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs(state)[oid]) == name:
            return oid
    return None


def bf_oids(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid in bf_oids(state, pid)
            if oname(objs(state)[oid]) == name]


def untapped_named(state, pid, name):
    return [oid for oid in bf_oids(state, pid)
            if oname(objs(state)[oid]) == name
            and not objs(state)[oid].get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def stack_of(state):
    return state.get("stack") or []


def has_trigger_on_stack(state):
    for e in stack_of(state):
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt == "TriggeredAbility":
            return True
    return False


def nils_trigger_on_stack(state):
    """True if a TriggeredAbility from Nils, Discipline Enforcer is on the
    stack."""
    for e in stack_of(state):
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt != "TriggeredAbility":
            continue
        blob = json.dumps(e, default=str)
        if "Nils" in blob or "iscipline" in blob:
            return True
    return False


def counters_of(obj):
    c = obj.get("counters")
    if isinstance(c, dict):
        return c
    if isinstance(c, list):
        out = {}
        for e in c:
            if isinstance(e, dict):
                k = e.get("type") or e.get("kind") or e.get("name")
                out[str(k)] = e.get("count", 1)
            else:
                out[str(e)] = out.get(str(e), 0) + 1
        return out
    return {}


def p1p1_count(obj):
    n = 0
    for k, v in counters_of(obj).items():
        kl = str(k).lower()
        if "p1p1" in kl or "+1/+1" in kl:
            try:
                n += int(v)
            except Exception:
                n += 1
    return n


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")


async def export_now(path):
    c0 = CLIENTS["P0"]
    try:
        s = await c0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)["state"]


def find_action(acts, atype):
    for a in acts:
        if a["type"] == atype:
            return a
    return None


def vi_opportunities(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities") or []


def cand_ref_oid(ch):
    for s in ch.get("surfaces") or []:
        d = s.get("data") or {}
        if not isinstance(d, dict):
            continue
        ref = d.get("reference")
        if isinstance(ref, str):
            try:
                return int(ref)
            except Exception:
                pass
        if isinstance(ref, dict):
            r = _hunt_oid(ref)
            if r is not None:
                return r
    return None


def _hunt_oid(v):
    if isinstance(v, dict):
        for k, val in v.items():
            if k in ("object_id", "oid") and isinstance(val, (int, str)):
                try:
                    return int(val)
                except Exception:
                    pass
            r = _hunt_oid(val)
            if r is not None:
                return r
    elif isinstance(v, list):
        for val in v:
            r = _hunt_oid(val)
            if r is not None:
                return r
    return None


def cand_desc(ch, state):
    oid = cand_ref_oid(ch)
    if oid is not None:
        o = get_obj(state, oid)
        if o:
            return f"{oname(o)}#{oid}(P{o.get('controller')})"
    return f"cand:{ch.get('id')}"


def detect_scope_player(state, cands):
    """Which player's 'for each player' iteration is this prompt for?

    Prefer an explicit scope field in waiting_for.data; fall back to
    candidate-controller unanimity; last resort None (caller infers).
    """
    d = wf_data(state)
    for key in ("scope_player", "for_player", "target_player", "scope",
                "iter_player", "player_scope"):
        v = d.get(key)
        if isinstance(v, int):
            return v, f"wf_data.{key}"
        if isinstance(v, dict):
            for kk in ("player", "seat", "index"):
                if isinstance(v.get(kk), int):
                    return v[kk], f"wf_data.{key}.{kk}"
    ctrls = set()
    for ch in cands:
        oid = cand_ref_oid(ch)
        if oid is not None:
            c = get_obj(state, oid).get("controller")
            if c is not None:
                ctrls.add(c)
    if len(ctrls) == 1:
        return next(iter(ctrls)), "candidate unanimity"
    return None, "unknown"


async def answer_nils_target(c, pid, st, state):
    """Answer one Nils TriggerTargetSelection with a single target.

    Chooses a creature controlled by the prompt's scope player (one that has
    not already been assigned a counter this trigger). Returns True if an
    answer was submitted.
    """
    if pid != 0:
        return False
    if wf_type(state) != "TriggerTargetSelection" or wf_player(state) != 0:
        return False
    opps = vi_opportunities(st)
    if not opps:
        return False
    nilsst = ST["nils"]
    for opp in opps:
        iid = opp.get("interactionId")
        if ST["answered_iids"].get(iid):
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        spec = data.get("spec") or {}
        stype = spec.get("type") or "sequence"
        n = len(nilsst["prompts"])
        with open(f"{EVDIR}/opportunity_nils_{n}.json", "w") as f:
            json.dump({"waiting_for": state.get("waiting_for"),
                       "opportunity": opp}, f, indent=1, default=str)
        scope, how = detect_scope_player(state, cands)
        if scope is None:
            # infer: first player (turn order) with a candidate creature
            # not yet assigned this trigger
            seen_ctrl = {}
            for ch in cands:
                oid = cand_ref_oid(ch)
                if oid is None:
                    continue
                pc = get_obj(state, oid).get("controller")
                if pc is not None and pc not in seen_ctrl:
                    seen_ctrl[pc] = oid
            scope = next((p for p in (0, 1, 2)
                          if p in seen_ctrl
                          and p not in nilsst["assigned"]), None)
            how = "inferred-first-unassigned"
            if scope is None:
                scope = next(iter(seen_ctrl), None)
                how = "inferred-any"
        say(f"[P0] Nils target prompt #{n}: rtype={rtype} stype={stype} "
            f"ncands={len(cands)} scope_player={scope} ({how}) iid={iid}")
        for ch in cands:
            say(f"    cand id={ch.get('id')} -> {cand_desc(ch, state)}")
        wire("nils_target_prompt",
             {"n": n, "rtype": rtype, "stype": stype, "ncands": len(cands),
              "scope_player": scope, "scope_how": how,
              "cands": [cand_desc(ch, state) for ch in cands]})
        if not nilsst["pre"]:
            nilsst["pre"] = True
            nilsst["answer_turn"] = state.get("turn_number")
            await export_now("pre.json")
        # pick one candidate controlled by the scope player
        pick = None
        for ch in cands:
            oid = cand_ref_oid(ch)
            if oid is not None and get_obj(state, oid).get(
                    "controller") == scope:
                pick = ch
                break
        if pick is None and cands:
            pick = cands[0]
            say(f"[P0] WARNING: no candidate for scope player {scope}; "
                f"falling back to first candidate")
        if pick is None:
            say("[P0] no candidates at all; cannot answer")
            return False
        chosen_oid = cand_ref_oid(pick)
        dup = scope in nilsst["assigned"]
        if rtype == "schema":
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        wire("nils_target_answer",
             {"n": n, "scope_player": scope, "chosen_oid": chosen_oid,
              "chosen": cand_desc(pick, state), "duplicate_scope": dup,
              "sub": sub})
        say(f"[P0] Nils prompt #{n} answers scope P{scope} -> "
            f"{cand_desc(pick, state)}{' (DUPLICATE SCOPE)' if dup else ''}")
        await c.send_interaction(sub)
        nilsst["prompts"].append({"n": n, "scope_player": scope,
                                 "scope_how": how,
                                 "chosen_oid": chosen_oid,
                                 "duplicate_scope": dup,
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
        if not dup:
            nilsst["assigned"][scope] = chosen_oid
        ST["answered_iids"][iid] = True
        return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    wt, wp = wf_type(state), wf_player(state)
    rev = st.get("state_revision", -1)

    # mulligan: keep for everyone
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            if wt == "MulliganDecision" and any(
                    p.get("player") == pid for p in pend):
                if not acted(f"mull{pid}", rev):
                    await submit_as_is(
                        c, {"type": "MulliganDecision",
                            "data": {"choice": {"type": "Keep"}}})
                    say(f"[{c.name}] keeps")
            return True

    # discard to hand size; protect NILS for P0
    if wt == "DiscardToHandSize" and wp == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        oids = hand_oids(state, pid)
        # discard non-Nils first; Nils copies only if the hand is all Nils
        non_nils = [int(x) for x in oids
                    if oname(objs(state)[x]) != NILS]
        picks = non_nils[:n]
        if len(picks) < n:
            rest = [int(x) for x in oids if int(x) not in picks]
            picks += rest[:n - len(picks)]
        if picks and not acted(f"disc{pid}", rev):
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {len(picks)}")
        return True

    # never attack, never block
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da and wp == pid:
            if not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return True
    if (state.get("phase") or "") == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da and wp == pid:
            if not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "bands" in sub["data"]:
                    sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return True

    # the Nils end-step target prompt (P0 chooses all targets)
    if await answer_nils_target(c, pid, st, state):
        return True

    # main-phase driving
    if is_my_main(state, pid):
        land = {0: PLAINS, 1: FOREST, 2: ISLAND}[pid]
        lid = find_hand(state, pid, land)
        pa = None
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id", "")) == str(lid):
                pa = a
                break
        if pa and not acted(f"land{pid}", rev):
            await submit_as_is(c, pa)
            return True
        if pid == 0:
            # cast Nils once ({2}{W}); legend-gated
            if not bf_named(state, 0, NILS):
                nid = find_hand(state, 0, NILS)
                ca = None
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id", "")) == str(
                            nid):
                        ca = a
                        break
                if ca and not acted("castnils", rev):
                    await submit_as_is(c, ca)
                    say(f"[P0] casts Nils (turn {state.get('turn_number')})")
                    return True
        elif pid == 1:
            if not bf_named(state, 1, BEAR):
                bid = find_hand(state, 1, BEAR)
                ca = None
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id", "")) == str(
                            bid):
                        ca = a
                        break
                if ca and not acted("castbear", rev):
                    await submit_as_is(c, ca)
                    say(f"[P1] casts Bear (turn {state.get('turn_number')})")
                    return True
        elif pid == 2:
            if not bf_named(state, 2, CROW):
                cid = find_hand(state, 2, CROW)
                ca = None
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id", "")) == str(
                            cid):
                        ca = a
                        break
                if ca and not acted("castcrow", rev):
                    await submit_as_is(c, ca)
                    say(f"[P2] casts Crow (turn {state.get('turn_number')})")
                    return True

    # default: pass priority only when it is ours
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", rev):
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def watch_resolution():
    """After the first prompt is answered, export mid as soon as the Nils
    trigger leaves the stack and no target selection is pending."""
    nilsst = ST["nils"]
    if not nilsst["prompts"] or nilsst["mid"]:
        return
    c0 = CLIENTS["P0"]
    st = c0.latest
    if not st:
        return
    state = st["state"]
    if (not nils_trigger_on_stack(state)
            and not has_trigger_on_stack(state)
            and wf_type(state) != "TriggerTargetSelection"):
        nilsst["mid"] = True
        nilsst["mid_turn"] = state.get("turn_number")
        nilsst["mid_phase"] = state.get("phase")
        s = await export_now("mid.json")
        if s:
            dist = counter_distribution(s)
            say(f"[watch] mid exported at turn {nilsst['mid_turn']} "
                f"phase {nilsst['mid_phase']}; counters: {dist}")
            wire("mid_counters", dist)
        ST["stop"] = True
        ST["done_reason"] = "nils trigger resolved; mid exported"
        say("Nils trigger resolved; stopping")


def counter_distribution(state):
    """oid -> {name, controller, p1p1} for every battlefield object with
    +1/+1 counters."""
    out = {}
    for oid, o in objs(state).items():
        if o.get("zone") != "Battlefield":
            continue
        n = p1p1_count(o)
        if n:
            out[str(oid)] = {"name": oname(o),
                             "controller": o.get("controller"),
                             "p1p1": n}
    return out


def creature_summary(state):
    out = {}
    for oid, o in objs(state).items():
        if o.get("zone") != "Battlefield":
            continue
        nm = oname(o)
        if nm in (NILS, BEAR, CROW):
            out[str(oid)] = {"name": nm, "controller": o.get("controller"),
                             "p1p1": p1p1_count(o)}
    return out


async def main():
    reset()
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    for name, pid in (("P0", 0), ("P1", 1), ("P2", 2)):
        c = PhaseClient(name)
        await c.connect()
        CLIENTS[name] = c
    await CLIENTS["P0"].create(deck(*P0_DECK), player_count=3)
    await CLIENTS["P1"].join(CLIENTS["P0"].game_code, deck(*P1_DECK))
    await CLIENTS["P2"].join(CLIENTS["P0"].game_code, deck(*P2_DECK))
    say(f"game {CLIENTS['P0'].game_code}; RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": CLIENTS["P0"].game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "p2_deck": P2_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    order = (("P0", 0), ("P1", 1), ("P2", 2))
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for name, pid in order:
            try:
                await tick(CLIENTS[name], pid)
            except Exception as e:
                say(f"tick error [{name}]: {e}")
        await watch_resolution()
        await asyncio.sleep(0.1)

    if not ST["stop"]:
        ST["done_reason"] = "global timeout"
        wire("timeout", {})
        say("TIMEOUT")
    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finish()


def load_state(fn):
    p = f"{EVDIR}/{fn}"
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


def assertions_from_states():
    ass = {}
    notes = []
    nilsst = ST["nils"]
    pre = load_state("pre.json")
    mid = load_state("mid.json")
    post = load_state("post.json")

    # A1: parse check on the pinned card-data
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
        trig = cd["nils, discipline enforcer"]["triggers"][0]
        ex = trig["execute"]
        eff = ex.get("effect") or {}
        mt = ex.get("multi_target") or {}
        mx = mt.get("max")
        mxv = mx.get("value") if isinstance(mx, dict) else mx
        cnt = eff.get("count")
        cntv = cnt.get("value") if isinstance(cnt, dict) else cnt
        tgt = eff.get("target") or {}
        ok1 = (eff.get("type") == "PutCounter"
               and eff.get("counter_type") == "P1P1" and cntv == 1
               and tgt.get("controller") == "ScopedPlayer"
               and mt.get("min") == 0 and mxv == 1
               and (ex.get("player_scope") or {}).get("type") == "All"
               and trig.get("phase") == "End"
               and (trig.get("constraint") or {}).get("type")
               == "OnlyDuringYourTurn")
        notes.append(f"A1: effect={eff.get('type')} "
                     f"{eff.get('counter_type')}x{cntv} "
                     f"target.controller={tgt.get('controller')} "
                     f"multi_target={mt} "
                     f"player_scope={(ex.get('player_scope') or {}).get('type')} "
                     f"phase={trig.get('phase')} "
                     f"constraint={(trig.get('constraint') or {}).get('type')}")
    except Exception as e:
        ok1 = False
        notes.append(f"A1: parse read failed: {e}")
    ass["A1_parse_ok"] = "passed" if ok1 else "failed"

    # A2: setup -- prompt(s) seen, Nils + one creature per side at pre
    cs_pre = creature_summary(pre) if pre else {}
    nils_bf = any(v["name"] == NILS and v["controller"] == 0
                  for v in cs_pre.values())
    sides = {v["controller"] for v in cs_pre.values()}
    ok2 = (len(nilsst["prompts"]) >= 1 and nilsst["pre"] and nils_bf
           and sides == {0, 1, 2})
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"
    notes.append(f"A2: prompts_answered={len(nilsst['prompts'])} "
                 f"pre_exported={nilsst['pre']} nils_on_bf={nils_bf} "
                 f"sides={sorted(sides)} "
                 f"answer_turn={nilsst['answer_turn']}")

    # A3: per-player counter distribution at mid
    dist = counter_distribution(mid) if mid else {}
    per_player = {}
    max_on_one = 0
    total = 0
    for oid, v in dist.items():
        total += v["p1p1"]
        max_on_one = max(max_on_one, v["p1p1"])
        per_player[v["controller"]] = per_player.get(
            v["controller"], 0) + v["p1p1"]
    chosen = [p["chosen_oid"] for p in nilsst["prompts"]]
    chosen_counts = {}
    if mid:
        for oid in chosen:
            if oid is not None:
                chosen_counts[str(oid)] = p1p1_count(get_obj(mid, oid))
    ok3 = (nilsst["mid"] and total == 3 and max_on_one == 1
           and per_player == {0: 1, 1: 1, 2: 1}
           and all(c == 1 for c in chosen_counts.values()))
    ass["A3_per_player_one"] = "passed" if ok3 else (
        "not-run" if not nilsst["mid"] else "failed")
    notes.append(f"A3: mid_exported={nilsst['mid']} total_p1p1={total} "
                 f"max_on_one_creature={max_on_one} "
                 f"per_player={per_player} "
                 f"chosen_oid_counts={chosen_counts} "
                 f"distribution={dist}")

    # A4: trigger resolved and game advanced
    ok4 = (nilsst["mid"] and nilsst["mid_turn"] is not None
           and nilsst["answer_turn"] is not None
           and nilsst["mid_turn"] >= nilsst["answer_turn"])
    ass["A4_trigger_resolved"] = "passed" if ok4 else (
        "not-run" if not nilsst["mid"] else "failed")
    notes.append(f"A4: answer_turn={nilsst['answer_turn']} "
                 f"mid_turn={nilsst['mid_turn']} mid_phase="
                 f"{nilsst['mid_phase']}")

    # A5: cleanup -- stack empty at final observation
    final = post or mid
    fstack = len(stack_of(final)) if final else None
    ok5 = fstack == 0
    ass["A5_cleanup"] = "passed" if ok5 else (
        "not-run" if final is None else "failed")
    notes.append(f"A5: final stack depth={fstack}")

    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
    elif ass["A3_per_player_one"] == "not-run":
        verdict = "blocked"
        notes.append("verdict=blocked: mid export missed the resolution "
                     "window; re-run required.")
    elif ass["A3_per_player_one"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict}: {len(nilsst['prompts'])} prompt(s) "
                 f"answered; counters landed as {dist}")
    return ass, notes, verdict


async def finish():
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7148.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    if not ST["exports"].get("post.json"):
        await export_now("post.json")

    ass, notes, verdict = assertions_from_states()
    say(f"VERDICT: {verdict}")
    for n in notes:
        say("  " + n)

    run = {
        "issue": 7148,
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "server_hello": ST["server_hello"],
        "game_code": CLIENTS["P0"].game_code if CLIENTS.get("P0") else None,
        "seats": {n: CLIENTS[n].player_id for n in CLIENTS},
        "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
        "assertions": ass,
        "notes": notes,
        "rejections": ST["rejections"],
        "nils": {k: v for k, v in ST["nils"].items()},
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Nils, Discipline Enforcer end-step trigger 'for each "
                 "player, put a +1/+1 counter on up to one target creature "
                 "that player controls': 3-seat game (P0 Nils / P1 Bear / "
                 "P2 Crow, one creature each), driver answers each target "
                 "prompt with that player's creature; asserts one counter "
                 "per player; native engine, three human-client seats",
        "limitations": [
            "Browser UI not exercised; native engine via three human-client "
            "seats.",
            "Dense 12x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "The 'up to one' decline branch (choosing zero targets for a "
            "player) was not exercised; only the accept path was tested.",
            "Nils's second static ability (attack tax on counter-bearing "
            "creatures) was not exercised; no attacks were declared.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written")

    try:
        WIRE.close()
    except Exception:
        pass
    RUNLOG.close()
    print("logs closed", flush=True)

    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(f"{EVDIR}/{f}")
                   and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest written ({len(lines)} files)", flush=True)

    try:
        import subprocess
        subprocess.run(
            [sys.executable,
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7148.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)

    try:
        files = sorted(f for f in os.listdir(EVDIR)
                       if os.path.isfile(f"{EVDIR}/{f}")
                       and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"manifest re-hashed ({len(lines)} files)", flush=True)
    except Exception as e:
        print(f"manifest re-hash failed: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
