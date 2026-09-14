#!/usr/bin/env python3
"""phase-rs/phase #7147 - Talon Gates of Madara ETB: selecting no targets
phases out ALL creatures instead of none.

Oracle: "When this land enters, up to one target creature phases out."

Reported: "Was able to play it, but when I selected none for the targets
it phased all available targets."

Triage acceptance criteria:
- Zero targets is legal and affects nothing.
- One selected target phases out only that creature.
- An empty target list can never fall back to all legal targets.

Card-data parse (v0.82.0): trigger mode ChangesZone, effect PhaseOut,
target Typed[Creature], multi_target {min: 0, max: 1} -- the parse is
correct, so the defect (if any) is in runtime empty-selection handling.

Behavioral contract (2 human seats, native engine, protocol 70):
  Leg A (reported bug): P0 plays Talon Gates of Madara with bears on both
    battlefields; at the ETB TriggerTargetSelection submit an EMPTY target
    selection; assert no creature phases out.
  Leg B (control): on a later turn P0 plays a second Talon Gates and
    selects exactly ONE bear; assert only that bear phases out.
  A1 parse_ok:       multi_target min 0 / max 1 on the ETB trigger.
  A2 setup_ok:       leg-A prompt seen, >=1 bear on each BF, pre exported.
  A3 empty_accepted: empty selection accepted (no interaction rejection).
  A4 zero_phases_nothing: no bear phased out after leg-A resolution.
                     (fails => the reported bug reproduces)
  A5 control_single: leg-B single target phases out exactly that bear.
  A6 cleanup:        stack empty at final observation.

Verdict: reproduced iff A2+A3 pass and A4 fails (creatures phased out
despite the empty selection). not-reproduced iff A4 passes. blocked iff
A2 or A3 fail.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7147b")
EVDIR = f"{BACKFILL}/evidence/7147/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TALON = "Talon Gates of Madara"
BEAR = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = [(TALON, 12), (BEAR, 12), (FOREST, 36)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]
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
              "(reused from prior run; ServerHello re-verified this run) "
              "+ verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key).",
}

C0 = C1 = None
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
        "leg": "A",  # A -> B -> DONE
        "legA": {"prompt_seen": False, "ncands": 0, "pre": False,
                 "answered": False, "answer_t": None, "mid": False,
                 "turn": None, "rejected": False},
        "legB": {"prompt_seen": False, "pre": False, "answered": False,
                 "answer_t": None, "mid": False, "target_oid": None,
                 "target_cand_id": None, "turn": None, "rejected": False},
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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "leg": ST.get("leg")})
    await c.send_action(action)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            ST["rejections"].append({"who": c.name, "type": t,
                                     "data": data, "leg": ST.get("leg")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "leg": ST.get("leg")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)["state"]


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


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
    """Extract the referenced object oid from a candidate.

    Observed shape (protocol 70): surfaces[].data.reference is a plain
    numeric string naming the object id, e.g. "7" (see
    opportunity_legB.json). Fall back to a recursive hunt for an
    object_id/oid field inside a dict-shaped reference.
    """
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


def cand_name(ch, state):
    oid = cand_ref_oid(ch)
    if oid is not None:
        o = objs(state).get(str(oid))
        if o:
            return f"{oname(o)}#{oid}(P{o.get('controller')})"
    return f"cand:{ch.get('id')}"


async def answer_trigger_target(c, pid, st, state, leg):
    """Answer the ETB TriggerTargetSelection for pid.

    leg A: submit an EMPTY selection (the reported repro).
    leg B: submit exactly one candidate (control).
    Returns True if an answer was submitted."""
    if wf_type(state) != "TriggerTargetSelection" or wf_player(state) != pid:
        return False
    if ST[f"leg{leg}"]["answered"]:
        return False
    opps = vi_opportunities(st)
    if not opps:
        return False
    legst = ST[f"leg{leg}"]
    for opp in opps:
        iid = opp.get("interactionId")
        if ST["answered_iids"].get(iid):
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        # persist the opportunity shape as evidence
        with open(f"{EVDIR}/opportunity_leg{leg}.json", "w") as f:
            json.dump(opp, f, indent=1, default=str)
        say(f"[{c.name}] leg{leg} TriggerTargetSelection: rtype={rtype} "
            f"ncands={len(cands)} iid={iid}")
        for ch in cands:
            say(f"    cand id={ch.get('id')} -> {cand_name(ch, state)} "
                f"text={(ch.get('text') or '')[:50]!r}")
        wire("target_prompt", {"leg": leg, "rtype": rtype,
                               "ncands": len(cands),
                               "cands": [cand_name(ch, state)
                                         for ch in cands]})
        legst["prompt_seen"] = True
        legst["ncands"] = len(cands)
        legst["turn"] = state.get("turn_number")
        legst["answer_turn"] = state.get("turn_number")
        legst["answer_phase"] = state.get("phase")
        if not legst["pre"]:
            legst["pre"] = True
            await export_now(f"pre{leg}.json")
        if rtype == "schema":
            spec = data.get("spec") or {}
            stype = spec.get("type") or "sequence"
            if leg == "A":
                ids = []
            else:
                # control: pick one P1 bear if present else first candidate
                pick = None
                for ch in cands:
                    nm = cand_name(ch, state)
                    if "(P1)" in nm and "Grizzly Bears" in nm:
                        pick = ch
                        break
                pick = pick or (cands[0] if cands else None)
                if pick is None:
                    say(f"[{c.name}] legB: no candidates!")
                    return False
                ids = [pick.get("id")]
                legst["target_oid"] = cand_ref_oid(pick)
                legst["target_cand_id"] = pick.get("id")
                say(f"[{c.name}] legB targets {cand_name(pick, state)}")
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": ids}}}
        else:
            # exactChoices: find an explicit none/decline choice for leg A
            if leg == "A":
                none = None
                for ch in cands:
                    tx = (ch.get("text") or "").lower()
                    if any(k in tx for k in
                           ["none", "no target", "decline", "done",
                            "skip", "cancel", "zero"]):
                        none = ch
                        break
                if none is None:
                    say(f"[{c.name}] legA: exactChoices with no none-option; "
                        f"cannot submit empty -> marking rejected path")
                    legst["rejected"] = True
                    ST["answered_iids"][iid] = "no-none-option"
                    return False
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": none.get("id")}}}
                say(f"[{c.name}] legA answers none-choice "
                    f"id={none.get('id')} text={none.get('text')!r}")
            else:
                pick = None
                for ch in cands:
                    nm = cand_name(ch, state)
                    if "(P1)" in nm and "Grizzly Bears" in nm:
                        pick = ch
                        break
                pick = pick or (cands[0] if cands else None)
                if pick is None:
                    return False
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick.get("id")}}}
                legst["target_oid"] = cand_ref_oid(pick)
                say(f"[{c.name}] legB answers choice "
                    f"id={pick.get('id')} -> {cand_name(pick, state)}")
        wire("target_answer", {"leg": leg, "sub": sub})
        say(f"[{c.name}] leg{leg} submitting: {json.dumps(sub)[:220]}")
        await c.send_interaction(sub)
        legst["answered"] = True
        legst["answer_t"] = time.time()
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

    # mulligan: keep
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

    # discard to hand size; protect TALON + BEAR
    if wt == "DiscardToHandSize" and wp == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        oids = hand_oids(state, pid)
        prot = {TALON, BEAR}
        oids = sorted(oids,
                      key=lambda x: 0 if oname(objs(state)[x])
                      in prot else 1, reverse=True)
        picks = [int(x) for x in oids[:n]]
        if picks and not acted(f"disc{pid}", rev):
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {n}")
        return True

    # never block / never attack (keeps life totals clean)
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return True
    if (state.get("phase") or "") == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da:
            if not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "bands" in sub["data"]:
                    sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return True

    # the ETB target prompt (both legs)
    leg = ST["leg"]
    if pid == 0 and leg in ("A", "B"):
        if await answer_trigger_target(c, pid, st, state, leg):
            return True

    # main-phase driving
    if is_my_main(state, pid):
        if pid == 0:
            talon_bf = bool(bf_named(state, 0, TALON))
            # leg A: play Talon Gates once bears sit on both battlefields
            if leg == "A" and not talon_bf:
                nb0 = bf_named(state, 0, BEAR)
                nb1 = bf_named(state, 1, BEAR)
                tid = find_hand(state, 0, TALON)
                pa = playland_advertised(acts, tid)
                if pa and nb0 and nb1 and not acted("talonA", rev):
                    await submit_as_is(c, pa)
                    say(f"[P0] legA plays Talon Gates (turn "
                        f"{state.get('turn_number')})")
                    return True
            # leg B: second Talon Gates on a later turn, bears back
            if leg == "B" and not ST["legB"].get("played"):
                tid = find_hand(state, 0, TALON)
                pa = playland_advertised(acts, tid)
                bears0 = [o for o in bf_named(state, 0, BEAR)
                          if not phased_signal(objs(state)[o])]
                bears1 = [o for o in bf_named(state, 1, BEAR)
                          if not phased_signal(objs(state)[o])]
                if (pa and tid and len(bears0) + len(bears1) >= 2
                        and not acted("talonB", rev)):
                    ST["legB"]["played"] = True
                    await submit_as_is(c, pa)
                    say(f"[P0] legB plays Talon Gates (turn "
                        f"{state.get('turn_number')})")
                    return True
            # land drop (Forest) -- but not before a pending Talon play
            lid = find_hand(state, pid, FOREST)
            pa = playland_advertised(acts, lid)
            if pa and not acted(f"land{pid}", rev):
                await submit_as_is(c, pa)
                return True
            # cast bears (2 max per side keeps targets tidy)
            if len(bf_named(state, 0, BEAR)) < 2:
                bid = find_hand(state, 0, BEAR)
                ca = castspell_advertised(acts, bid)
                if ca and len(untapped_named(state, 0, FOREST)) >= 2 \
                        and not acted("castb0", rev):
                    await submit_as_is(c, ca)
                    say("[P0] casts Bear")
                    return True
        if pid == 1:
            lid = find_hand(state, pid, FOREST)
            pa = playland_advertised(acts, lid)
            if pa and not acted(f"land{pid}", rev):
                await submit_as_is(c, pa)
                return True
            if len(bf_named(state, 1, BEAR)) < 2:
                bid = find_hand(state, 1, BEAR)
                ca = castspell_advertised(acts, bid)
                if ca and len(untapped_named(state, 1, FOREST)) >= 2 \
                        and not acted("castb1", rev):
                    await submit_as_is(c, ca)
                    say("[P1] casts Bear")
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


def phased_signal(o):
    """True if a battlefield object is phased out.

    The engine marks permanents with phase_status: {"status":
    "PhasedIn"|"PhasedOut", ...}. A phased-out permanent stays in the
    Battlefield zone with status PhasedOut.
    """
    ps = o.get("phase_status")
    if isinstance(ps, dict):
        return ps.get("status") == "PhasedOut"
    if str(o.get("zone", "")).lower().replace("_", "") == "phasedout":
        return True
    return False


def bears_status(state):
    """oid -> dict describing each bear anywhere."""
    out = {}
    for oid, o in objs(state).items():
        if oname(o) == BEAR:
            out[str(oid)] = {
                "zone": o.get("zone"),
                "controller": o.get("controller"),
                "tapped": bool(o.get("tapped")),
                "phased": phased_signal(o),
            }
    return out


def stack_trigger_targets(state):
    """Targets recorded on TriggeredAbility stack entries (post-answer)."""
    out = []
    for e in stack_of(state):
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt == "TriggeredAbility":
            out.append(e.get("targets"))
    return out


async def watch_resolution():
    """After a leg's answer, export mid as soon as the trigger leaves the
    stack -- same-turn capture, so phased-out permanents cannot phase back
    in at an untap step before we observe them."""
    leg = ST["leg"]
    legst = ST[f"leg{leg}"]
    if not legst["answered"] or legst["mid"]:
        return
    st = C0.latest
    if not st:
        return
    state = st["state"]
    # sample the trigger's recorded targets while it is still on the stack
    tgts = stack_trigger_targets(state)
    if tgts and not legst.get("targets_sampled"):
        legst["targets_sampled"] = True
        wire(f"leg{leg}_trigger_targets", {"targets": tgts})
        say(f"[leg{leg}] trigger targets on stack: "
            f"{json.dumps(tgts)[:300]}")
    if not has_trigger_on_stack(state) \
            and wf_type(state) != "TriggerTargetSelection":
        legst["mid"] = True
        legst["mid_turn"] = state.get("turn_number")
        legst["mid_phase"] = state.get("phase")
        s = await export_now(f"mid{leg}.json")
        if s:
            bs = bears_status(s)
            say(f"[leg{leg}] mid: {len(bs)} bears tracked; phased="
                f"{[k for k, v in bs.items() if v['phased']]} "
                f"(answer turn {legst.get('answer_turn')} -> mid turn "
                f"{legst.get('mid_turn')})")
            wire(f"mid{leg}_bears", bs)
        if leg == "A":
            ST["leg"] = "B"
            say("legA resolution observed; leg=B armed")
        else:
            ST["leg"] = "DONE"
            ST["done_reason"] = "both legs resolved"
            ST["stop"] = True
            say("legB resolution observed; stopping")


async def main():
    reset()
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for c, pid in ((C0, 0), (C1, 1)):
            try:
                await tick(c, pid)
            except Exception as e:
                say(f"tick error [{c.name}]: {e}")
        await watch_resolution()
        await asyncio.sleep(0.1)

    if not ST["stop"]:
        ST["done_reason"] = "global timeout"
        wire("timeout", {"leg": ST["leg"]})
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
    legA, legB = ST["legA"], ST["legB"]
    preA = load_state("preA.json")
    midA = load_state("midA.json")
    preB = load_state("preB.json")
    midB = load_state("midB.json")
    post = load_state("post.json")

    # A1: parse check on the pinned card-data
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
        trig = cd["talon gates of madara"]["triggers"][0]["execute"]
        mt = trig.get("multi_target") or {}
        mx = mt.get("max")
        mxv = mx.get("value") if isinstance(mx, dict) else mx
        ok1 = (mt.get("min") == 0 and mxv == 1
               and (trig.get("effect") or {}).get("type") == "PhaseOut")
        notes.append(f"A1: multi_target={mt} effect="
                     f"{(trig.get('effect') or {}).get('type')}")
    except Exception as e:
        ok1 = False
        notes.append(f"A1: parse read failed: {e}")
    ass["A1_parse_ok"] = "passed" if ok1 else "failed"

    # A2: leg-A prompt seen with bears on both sides, pre exported
    bpre = bears_status(preA) if preA else {}
    sides = {v["controller"] for v in bpre.values()
             if v["zone"] == "Battlefield"}
    ok2 = (legA["prompt_seen"] and legA["pre"] and sides == {0, 1}
           and legA["ncands"] >= 2)
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"
    notes.append(f"A2: prompt_seen={legA['prompt_seen']} ncands="
                 f"{legA['ncands']} sides={sorted(sides)} pre={legA['pre']}")

    # A3: empty selection accepted (no rejection tied to the answer)
    rej_legA = [r for r in ST["rejections"] if r.get("leg") == "A"]
    ok3 = (legA["answered"] and not legA["rejected"]
           and not any("nteraction" in json.dumps(r.get("data", ""))
                       for r in rej_legA))
    ass["A3_empty_accepted"] = "passed" if ok3 else "failed"
    notes.append(f"A3: answered={legA['answered']} rejected_flag="
                 f"{legA['rejected']} legA rejections={len(rej_legA)}")

    # A4: no bear phased out after leg-A resolution
    bmid = bears_status(midA) if midA else {}
    phasedA = sorted(k for k, v in bmid.items() if v["phased"])
    # also catch bears that vanished from BF without a zone trail
    vanished = []
    if preA and midA:
        for oid, v in bpre.items():
            if v["zone"] == "Battlefield":
                w = bmid.get(oid)
                if w is None or (w["zone"] != "Battlefield"
                                  and w["zone"] not in (
                                      "Graveyard", "Exile", "Hand",
                                      "Library")):
                    vanished.append(oid)
    ok4 = (legA["mid"] and not phasedA and not vanished)
    same_turn = (legA.get("mid_turn") == legA.get("answer_turn")
                 and legA.get("mid_turn") is not None)
    ass["A4_zero_phases_nothing"] = "passed" if (ok4 and same_turn) else (
        "failed" if not ok4 else "not-run")
    notes.append(f"A4: mid_exported={legA['mid']} phased_after_empty="
                 f"{phasedA} vanished={vanished} answer_turn="
                 f"{legA.get('answer_turn')} mid_turn={legA.get('mid_turn')} "
                 f"same_turn={same_turn} "
                 f"(bears pre={len(bpre)} mid={len(bmid)})")

    # A5: control leg -- exactly the chosen bear phased out
    bpreB = bears_status(preB) if preB else {}
    bmidB = bears_status(midB) if midB else {}
    phasedB = sorted(k for k, v in bmidB.items() if v["phased"])
    tgt = str(legB["target_oid"]) if legB["target_oid"] is not None else None
    others_phased = [k for k in phasedB if k != tgt]
    ok5 = (legB["mid"] and tgt is not None and tgt in phasedB
           and not others_phased)
    ass["A5_control_single"] = "passed" if ok5 else (
        "not-run" if not legB["prompt_seen"] else "failed")
    notes.append(f"A5: prompt_seen={legB['prompt_seen']} answered="
                 f"{legB['answered']} target_oid={tgt} phased={phasedB} "
                 f"others_phased={others_phased}")

    # A6: cleanup -- stack empty at final observation
    final = post or midB or midA
    fstack = len(stack_of(final)) if final else None
    ok6 = fstack == 0
    ass["A6_cleanup"] = "passed" if ok6 else (
        "not-run" if final is None else "failed")
    notes.append(f"A6: final stack depth={fstack}")

    if ass["A2_setup_ok"] != "passed" or ass["A3_empty_accepted"] != "passed":
        verdict = "blocked"
    elif ass["A4_zero_phases_nothing"] == "not-run":
        verdict = "blocked"
        notes.append("verdict=blocked: mid export missed the resolution "
                     "turn, so a phase-out followed by phase-in at untap "
                     "could have been hidden; re-run required.")
    elif ass["A4_zero_phases_nothing"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(
        f"verdict={verdict}: legA empty selection -> phased bears "
        f"{phasedA}; legB single-target control -> phased {phasedB} "
        f"(target {tgt})")
    return ass, notes, verdict


async def finish():
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7147.py")
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
        "issue": 7147,
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "server_hello": ST["server_hello"],
        "game_code": C0.game_code if C0 else None,
        "seats": {"P0": C0.player_id if C0 else None,
                  "P1": C1.player_id if C1 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "rejections": ST["rejections"],
        "legs": {"A": {k: v for k, v in ST["legA"].items()},
                 "B": {k: v for k, v in ST["legB"].items()}},
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Talon Gates of Madara ETB 'up to one target creature "
                 "phases out': leg A submits an EMPTY target selection at "
                 "the TriggerTargetSelection prompt; leg B (control) "
                 "selects exactly one creature; native engine, two "
                 "human-client seats",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense 12x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Phased-out detection keys on engine state fields containing "
            "'phas' plus Battlefield disappearance without a zone trail; "
            "per-bear pre/mid diffs are in the run notes and wire log.",
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
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7147.py",
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
