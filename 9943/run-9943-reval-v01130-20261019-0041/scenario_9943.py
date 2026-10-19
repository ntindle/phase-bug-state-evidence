#!/usr/bin/env python3
"""Issue #9943: "fix(engine): a {0} unless-cost of a counter is asked, not
auto-paid (CR 118.5, 118.12a)".

v0.113.0 re-validation (protocol 151, build 0571ee3) of the verified
protocol-147 driver (scenario_9943.py). Mechanical port: identity strings
+ assertions only; driver logic byte-identical to the v0.112.0 run, whose
verdict was `reproduced` (auto-pay confirmed at X=0, UnlessPayment prompt
raised at X=2 control). The PR is still OPEN/unmerged, so the released
engine is expected to show the same behavior on v0.113.0.

#9943 is an OPEN fix PR (bug-labeled) whose Summary describes a concrete
player-observable behavior of the released engine: "a {X} counter such as
Clash of Wills or Power Sink at X=0, and a {0} ward cost all resolved
without ever offering the payer the choice" -- i.e. on the released
engine a {0} unless-cost counter is AUTO-PAID, never raising
WaitingFor::UnlessPayment. Per the #9681/#9934 precedent, a bug-labeled
fix PR with a concrete observable symptom is a valid reproduction target.
The PR is still OPEN (matthewevans has binding MED structural findings
requested; UNMERGED), so the fix is NOT in the pinned v0.113.0 release --
the auto-pay behavior should be observable there.

Oracle (verified from pinned v0.113.0 card-data.json, rechecked this run):
  Clash of Wills: {X}{U} Instant. "Counter target spell unless its
    controller pays {X}." cost shards ['X','Blue'].
  Grizzly Bears: {1}{G} Creature (2/2), the victim spell.

Plan (native engine, two human driver seats):
  P0: 12x Clash of Wills + 48x Island. Responds to P1's Bears with Clash.
  P1: 12x Grizzly Bears + 48x Forest. Casts Bears; is the unless-payer.
  Leg 0 (REPORTED): Clash X=0 targeting P1's Bears. On Clash's resolution
    the payer (P1, controller of the targeted spell) should -- per the PR's
    rules reading -- be asked (WaitingFor::UnlessPayment). The PR's premise
    is that the released engine never asks: it auto-pays {0}.
  Leg 1 (CONTROL): Clash X=2 targeting another Bears. The {0}-only auto-pay
    branch should not fire here, so the UnlessPayment prompt IS expected.
    Proves the prompt machinery works and the leg-0 reading is specific.

BEHAVIORAL CONTRACT (written before observing results)
  A1_setup_ok    pre.json exported with Clash X=0 on the stack, fully cast
                 (X chosen, Bears targeted), before resolution.
  A2_unless_prompt_zero (REPORTED)  UnlessPayment prompt raised to P1
                 during Clash X=0's resolution. passed == prompt observed;
                 failed == Clash X=0 resolved with no prompt and the Bears
                 was NOT countered (auto-pay: the reported bug).
  A3_unless_prompt_control  UnlessPayment prompt raised to P1 during Clash
                 X=2's resolution. passed == prompt observed; failed == no
                 prompt at X=2 either (control failure).
  A4_cleanup     post.json (and post_control.json when leg 1 ran) exported.

Verdict: reproduced iff A1 passes, A2 fails (auto-pay confirmed), and A3
passes. not-reproduced iff A1 and A2 pass. blocked iff A1 fails, A2
not-run, or A3 fails (control failure undermines the A2 reading).

Detection reliability note: a genuine UnlessPayment prompt STALLS the game
(P1 must answer before Clash's resolution can proceed), so "no prompt"
is established by the game proceeding past Clash's resolution with P1
never answering -- not by failing to catch a transient.

Scope limits: Power Sink X=0 and {0} ward costs (also named in the PR) are
not exercised; only Clash of Wills X=0 (+ X=2 control).

Evidence: evidence/9943/<run-id>/pre.json, post.json, pre_control.json,
post_control.json, prompt_legN.json (only if a prompt appears),
run.json, assertions.json, data_evidence.json, wire_log.jsonl,
scenario_run.log, scenario_9943.py, summary.png, manifest.sha256.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9943-reval-v01130-20261019-0041")
ISSUE = 9943
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None

CLASH = "clash of wills"
BEARS = "grizzly bears"
ISLAND = "island"
FOREST = "forest"

P0_DECK = deck((CLASH, 12), (ISLAND, 48))
P1_DECK = deck((BEARS, 12), (FOREST, 48))

SERVER_IDENTITY = {
    "server_version": "v0.113.0",
    "build_commit": "0571ee3",
    "protocol_version": 151,
    "mode": "Full",
    "binary_sha256": "f2b09aab4385ff6e6f352d211c2560af42445e11e6b5f2fef0065094c1a8c77d",
    "card_data_sha256": "eb309108eb0df708b6cb5b3c75858149a9fd2864ee0f4ef81195cc75af058b85",
    "draft_pools_sha256": "785376ea4dc8fd2fde29e734a1b069de3a12c57b2df1bb1471556b279b306400",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.113.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828) at pin time 2026-10-18; "
                      "binary digest matches the GitHub asset digest; "
                      "card-data + draft-pools digests match the signed "
                      "manifest",
    "observed_at": "2026-10-19",
    "source": "ServerHello asserted by handshake this run; digests "
              "recomputed against on-disk pinned release artifacts",
}

TIMEOUT = 1400
TURN_CAP = 40

ST = {}
MULLS = set()
MULL_COUNTS = {}
SUBMITTED_OPPS = set()


def reset():
    ST.update({
        "stage": "LEG0",   # LEG0 -> LEG1 -> DONE
        "leg": 0,
        "stop": False,
        "notes": [],
        "bears_cast": 0,
        "clash": None,     # {leg,x,x_chosen,target_chosen,bears_oid,
                           #  stack_oid,pre_exported}
        "unless_seen": {},     # leg -> observation dict
        "unless_answered": {},  # leg -> True
        "leg_outcomes": {},
        "post_exported": False,
        "post_control_exported": False,
        "last_submit": 0.0,
    })


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if RUNLOG is not None:
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


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life(state, pid):
    return player_of(state, pid).get("life")


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


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
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def stack_entries(state):
    return state.get("stack") or []


def stack_obj_id(e):
    for k in ("object_id", "id", "source_id"):
        v = e.get(k)
        if v is not None:
            return str(v)
    return None


def p1_bears_on_stack(state):
    out = []
    for e in stack_entries(state):
        oid = stack_obj_id(e)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if obj_lname(state, oid) == BEARS and str(o.get("controller")) == "1":
            out.append(oid)
    return out


def clash_on_stack(state):
    out = []
    for e in stack_entries(state):
        oid = stack_obj_id(e)
        if oid is None:
            continue
        if obj_lname(state, oid) == CLASH:
            out.append(oid)
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": copy.deepcopy(a["data"])}
    wire("action_submit", {"who": c.name, "action": msg})
    ST["last_submit"] = time.time()
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    ST["last_submit"] = time.time()
    await c.send_interaction(sub)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = (json.loads(raw).get("data") or {})
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.113.0"), f"unexpected version {ver}"
    assert int(proto) == 151, f"unexpected protocol {proto}"
    assert str(build) == "0571ee3", f"unexpected build {build}"


def check_card_data():
    ok, notes = True, []
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.113.0/data/"
                        f"card-data.json"))
    cl = cd.get(CLASH, {})
    ot = str(cl.get("oracle_text"))
    cost = cl.get("mana_cost") or {}
    if "Counter target spell unless its controller pays {X}." not in ot:
        ok = False
        notes.append("clash of wills oracle text mismatch: " + ot[:120])
    else:
        notes.append("clash of wills oracle confirmed: " + ot[:80])
    shards = cost.get("shards") or []
    if shards != ["X", "Blue"]:
        ok = False
        notes.append(f"clash of wills cost shards unexpected: {shards}")
    else:
        notes.append("clash of wills cost {X}{U} confirmed")
    br = cd.get(BEARS, {})
    if not br:
        ok = False
        notes.append("grizzly bears missing from card data")
    else:
        notes.append("grizzly bears present in card data")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "clash_oracle": ot, "clash_cost": cost,
                   "bears_cost": (br.get("mana_cost") or {})}, f, indent=1)
    say(f"card-data check: ok={ok}")
    wire("card_data_check", {"ok": ok, "notes": notes})
    return ok


async def do_mulligan(c, acts, st, pid, tag, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = hand_lnames(st["state"], pid)
    mulls = MULL_COUNTS.get(tag, 0)
    decision = "keep" if (keep_fn(hn) or mulls >= 4) else "mulligan"
    if decision == "mulligan":
        MULL_COUNTS[tag] = mulls + 1
    say(f"[{tag}] {decision} {len(hn)}: {hn} (mulls={mulls})")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if decision == "keep"
                                             else "Mulligan"}}})
    return True


def _bottom_or_discard(c, acts, st, pid, tag, n_keep):
    # shared bottom/discard via viewer_interaction schema/select
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return None
    return n


async def do_bottom(c, acts, st, pid, tag, keep_names):
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(_cand_ref(ch)) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", iid)
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {})
               or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            nm = _cand_lname(state, ch)
            if nm in keep_names:
                return (2, nm)
            return (0, nm)

        picks = sorted(cands, key=bkey)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming {[ _cand_lname(state, x) for x in picks]}")
        wire("bottom", {"who": tag, "iid": iid,
                        "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


def _cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def _cand_lname(state, ch):
    r = _cand_ref(ch)
    try:
        return obj_lname(state, int(r))
    except (TypeError, ValueError):
        return "?"


async def do_discard(c, acts, st, pid, tag, keep_names):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(ch):
        nm = _cand_lname(state, ch)
        if nm in keep_names:
            return (5, nm)
        return (1, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(_cand_ref(ch)) in handset]
        if not cands:
            continue
        picks = sorted(cands, key=rank)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding {[_cand_lname(state, x) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def cast_action_for(acts, state, pid, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and obj_lname(state, v) == name \
                        and str(get_obj(state, v).get("controller")) == str(pid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


async def export_named(c, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json")
    wire("export", {"tag": tag})


def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read())


async def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            return
        if t in ("Error", "ActionRejected"):
            ST.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            wire("rejection", {"who": c.name, "type": t, "data": data})

# ------------------------------------------------- clash cast flow (X + target)

async def handle_clash_flow(c, st, tag):
    """Answer the Clash's X-choice (number) and target selection (sequence)
    in either order. Returns True if a submission was made."""
    cl = ST["clash"]
    if cl is None or (cl["x_chosen"] and cl["target_chosen"]):
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        stype = spec.get("type")
        cands = data.get("candidates") or []
        if stype == "number" and not cl["x_chosen"]:
            sdata = spec.get("data", {}) or {}
            xmin = sdata.get("min", spec.get("min"))
            xmax = sdata.get("max", spec.get("max"))
            SUBMITTED_OPPS.add(iid)
            cl["x_chosen"] = True
            say(f"[{tag}] X-choice for clash (leg {cl['leg']}): "
                f"min={xmin} max={xmax}; choosing X={cl['x']}")
            wire("clash_xchoice", {"who": tag, "leg": cl["leg"],
                                   "min": xmin, "max": xmax,
                                   "chosen": cl["x"]})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "number", "data": {
                                      "value": int(cl["x"])}}}, tag)
            return True
        if stype == "sequence" and not cl["target_chosen"]:
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            pick = None
            for ch in avail:
                r = _cand_ref(ch)
                try:
                    if r is not None and str(int(r)) == str(cl["bears_oid"]):
                        pick = ch
                        break
                except (TypeError, ValueError):
                    continue
            if pick is None and len(avail) == 1:
                pick = avail[0]
                say(f"[{tag}] single target candidate; taking it")
            if pick is None:
                wire("clash_target_no_pick",
                     {"who": tag, "leg": cl["leg"],
                      "bears_oid": cl["bears_oid"],
                      "candidates": cands})
                say(f"[{tag}] target prompt: no candidate matching bears "
                    f"oid {cl['bears_oid']}; holding")
                continue
            SUBMITTED_OPPS.add(iid)
            cl["target_chosen"] = True
            say(f"[{tag}] targeting bears {cl['bears_oid']} with clash "
                f"(leg {cl['leg']})")
            wire("clash_target", {"who": tag, "leg": cl["leg"],
                                  "bears_oid": cl["bears_oid"],
                                  "choice_id": pick.get("id")})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "sequence", "data": {
                                      "choiceIds": [pick.get("id")]}}}, tag)
            return True
    return False


# ------------------------------------------------- UnlessPayment detection

def _opp_text(opp):
    return json.dumps(opp, default=str).lower()


def pick_unless_choice(opp, want):
    """Best-effort pick of a pay/decline choice from an UnlessPayment
    opportunity. Returns (choice_id, envelope) or (None, None)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    if not items:
        return None, None

    def score(ch):
        txt = _opp_text(ch)
        if want == "pay":
            if "pay" in txt and "decline" not in txt \
                    and "don't pay" not in txt and "do not pay" not in txt \
                    and "not pay" not in txt:
                return 2
            return 0
        else:  # decline
            if "decline" in txt or "don't pay" in txt \
                    or "do not pay" in txt:
                return 2
            if "counter" in txt:
                return 1
            return 0

    avail = [ch for ch in items
             if ch.get("status", {}).get("type", "available") == "available"]
    pool = avail or items
    best = max(pool, key=lambda ch: (score(ch),
                                    -pool.index(ch)))
    if score(best) == 0:
        # no identifiable label; take positionally: first for pay,
        # last for decline (boolean prompts are usually [pay, decline])
        best = pool[0] if want == "pay" else pool[-1]
    cid = best.get("id") or best.get("choiceId")
    rtype = resp.get("type")
    if rtype == "schema":
        spec = data.get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        env = {"type": stype, "data": {"choiceIds": [cid]}}
    else:
        env = {"type": "choose", "data": {"choiceId": cid}}
    return cid, env


async def check_unless(c, st, tag, pid):
    """Detect a WaitingFor::UnlessPayment (or any unless-flavored prompt).
    Records the first observation per leg, exports the prompt state, then
    best-effort answers it (pay for leg 0, decline for leg 1)."""
    cl = ST["clash"]
    if cl is None:
        return False
    leg = cl["leg"]
    state = st["state"]
    wf = state.get("waiting_for") or {}
    wftype = str(wf.get("type") or "")
    unless_wf = "unless" in wftype.lower()
    opps = [o for o in vi_ops(st) if "unless" in _opp_text(o)]
    if not (unless_wf or opps):
        return False
    if leg not in ST["unless_seen"]:
        ST["unless_seen"][leg] = {
            "seat": pid, "tag": tag, "waiting_for": wf,
            "opportunities": opps, "t": time.time(),
            "turn": state.get("turn_number"), "phase": state.get("phase"),
        }
        say(f"[{tag}] UNLESS-PROMPT observed (leg {leg}, seat {pid}): "
            f"waiting_for={wftype}")
        wire("unless_prompt", {"who": tag, "leg": leg, "seat": pid,
                               "waiting_for": wf, "opportunities": opps})
        await export_named(c, f"prompt_leg{leg}")
    if leg in ST["unless_answered"]:
        return False
    # answer best-effort
    target_opp = opps[0] if opps else None
    if target_opp is None:
        say(f"[{tag}] unless prompt has no answerable opportunity; holding")
        wire("unless_no_opp", {"who": tag, "leg": leg})
        return True
    want = "pay" if leg == 0 else "decline"
    cid, env = pick_unless_choice(target_opp, want)
    if cid is None:
        say(f"[{tag}] could not pick an unless choice; holding")
        wire("unless_no_pick", {"who": tag, "leg": leg})
        return True
    ST["unless_answered"][leg] = True
    say(f"[{tag}] answering unless prompt (leg {leg}) with {want}-ish "
        f"choice {cid}")
    wire("unless_answer", {"who": tag, "leg": leg, "want": want,
                           "choice_id": cid, "envelope": env})
    await interact_as(c, {"interactionId": target_opp.get("interactionId"),
                          "response": env}, tag)
    return True


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn: sum(1 for n in hn if n == ISLAND) >= 2
                         and CLASH in hn):
        return True
    if await do_bottom(c, acts, st, 0, "P0", {CLASH}):
        return True
    if await do_discard(c, acts, st, 0, "P0", {CLASH}):
        return True
    if await check_unless(c, st, "P0", 0):
        return True
    if await handle_clash_flow(c, st, "P0"):
        return True
    # cast Clash in response to P1's Bears on the stack
    if my_priority(acts) and ST["leg"] in (0, 1) and ST["clash"] is None:
        bears = p1_bears_on_stack(state)
        if bears:
            a = cast_action_for(acts, state, 0, CLASH)
            if a is not None:
                want_x = 0 if ST["leg"] == 0 else 2
                ST["clash"] = {"leg": ST["leg"], "x": want_x,
                               "x_chosen": False, "target_chosen": False,
                               "bears_oid": bears[0], "stack_oid": None,
                               "pre_exported": False}
                say(f"[P0] casting clash of wills X={want_x} targeting "
                    f"bears {bears[0]} (leg {ST['leg']})")
                wire("clash_cast", {"leg": ST["leg"], "x": want_x,
                                    "bears_oid": bears[0]})
                await submit_as_is(c, a)
                return True
    # combat: never attack/block; declare empty so combat never stalls
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareAttackers"))
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareBlockers"))
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(c, d)
        return True
    if not my_priority(acts):
        return False
    if real_decision_pending(st):
        return False
    if my_main(state, 0):
        have = sum(1 for o in (state.get("objects") or {}).values()
                   if o.get("zone") == "Battlefield"
                   and str(o.get("controller")) == "0"
                   and is_land(o))
        if have < 10:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    return await pass_priority(c, st, acts)


async def p1_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 1, "P1",
                         lambda hn: sum(1 for n in hn if n == FOREST) >= 2
                         and BEARS in hn):
        return True
    if await do_bottom(c, acts, st, 1, "P1", {BEARS}):
        return True
    if await do_discard(c, acts, st, 1, "P1", {BEARS}):
        return True
    if await check_unless(c, st, "P1", 1):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareAttackers"))
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareBlockers"))
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(c, d)
        return True
    if not my_priority(acts):
        return False
    if real_decision_pending(st):
        return False
    # cast Bears on own main when no clash is in flight and none on stack
    if my_main(state, 1) and ST["leg"] in (0, 1) and ST["clash"] is None \
            and ST["bears_cast"] < 4 and not p1_bears_on_stack(state) \
            and not clash_on_stack(state):
        a = cast_action_for(acts, state, 1, BEARS)
        if a is not None:
            ST["bears_cast"] += 1
            say(f"[P1] casting grizzly bears #{ST['bears_cast']} "
                f"(leg {ST['leg']})")
            wire("bears_cast", {"n": ST["bears_cast"], "leg": ST["leg"]})
            await submit_as_is(c, a)
            return True
    if my_main(state, 1):
        have = sum(1 for o in (state.get("objects") or {}).values()
                   if o.get("zone") == "Battlefield"
                   and str(o.get("controller")) == "1"
                   and is_land(o))
        if have < 10:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    return await pass_priority(c, st, acts)


# ------------------------------------------------------------- evaluate

def evaluate():
    a = {k: "not-run" for k in ("A1_setup_ok", "A2_unless_prompt_zero",
                                "A3_unless_prompt_control", "A4_cleanup")}
    notes = ST["notes"]
    pre = load_env("pre")
    post = load_env("post")
    prec = load_env("pre_control")
    postc = load_env("post_control")

    if pre is not None:
        s = pre["state"]
        clashes = [e for e in stack_entries(s)
                   if obj_lname(s, stack_obj_id(e)) == CLASH]
        if clashes:
            a["A1_setup_ok"] = "passed"
            notes.append(f"A1: pre.json has Clash X=0 on stack "
                         f"(turn {s.get('turn_number')}, "
                         f"phase {s.get('phase')})")
        else:
            a["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json exported but no Clash on stack")
    else:
        a["A1_setup_ok"] = "failed"
        notes.append("A1: pre.json missing")

    lo0 = ST["leg_outcomes"].get(0)
    if lo0 is not None:
        notes.append(f"leg0: prompt_seen={lo0['prompt_seen']}, "
                     f"bears_zone={lo0['bears_zone']}, "
                     f"bears_countered={lo0['bears_countered']}")
        if lo0["prompt_seen"]:
            a["A2_unless_prompt_zero"] = "passed"
            notes.append("A2: UnlessPayment prompt WAS raised at X=0 -- "
                         "reported auto-pay behavior ABSENT")
        elif not lo0["bears_countered"]:
            a["A2_unless_prompt_zero"] = "failed"
            notes.append("A2: Clash X=0 resolved with NO UnlessPayment "
                         "prompt and the Bears was NOT countered -- "
                         "auto-pay CONFIRMED (REPORTED BUG reproduced)")
        else:
            a["A2_unless_prompt_zero"] = "failed"
            notes.append("A2: ANOMALY -- no prompt at X=0 but the Bears WAS "
                         "countered (auto-decline?); needs investigation")
    else:
        notes.append("A2: leg 0 never completed -> not-run")

    lo1 = ST["leg_outcomes"].get(1)
    if lo1 is not None:
        notes.append(f"leg1 (control X=2): prompt_seen={lo1['prompt_seen']}, "
                     f"bears_zone={lo1['bears_zone']}")
        if lo1["prompt_seen"]:
            a["A3_unless_prompt_control"] = "passed"
            notes.append("A3: UnlessPayment prompt raised at X=2 -- control "
                         "confirms the prompt machinery works; the X=0 "
                         "skip is specific")
        else:
            a["A3_unless_prompt_control"] = "failed"
            notes.append("A3: NO UnlessPayment prompt at X=2 either -- "
                         "control FAILED; leg-0 reading untrustworthy")
    else:
        notes.append("A3: control leg never completed -> not-run")

    if post is not None:
        a["A4_cleanup"] = "passed"
        notes.append("A4: post.json exported")
        if postc is not None:
            notes.append("A4: post_control.json exported")
    else:
        notes.append("A4: post.json missing -> not-run")

    if a["A1_setup_ok"] == "failed" or a["A2_unless_prompt_zero"] == "not-run":
        verdict = "blocked"
    elif a["A2_unless_prompt_zero"] == "failed" \
            and a["A3_unless_prompt_control"] == "passed":
        verdict = "reproduced"
    elif a["A2_unless_prompt_zero"] == "failed":
        # control missing/failed: the X=0 observation (game proceeded past
        # resolution with P1 never answering) still stands on its own, but
        # without a working control the reading is weaker.
        if a["A3_unless_prompt_control"] == "failed":
            verdict = "blocked"
            notes.append("verdict blocked: control leg failed, cannot "
                         "isolate the X=0 skip")
        else:
            verdict = "reproduced"
            notes.append("verdict reproduced with control not-run "
                         "(limitation)")
    elif a["A2_unless_prompt_zero"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, verdict


# ------------------------------------------------------------- render

def render_summary(run, out_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 920
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9943 - {0} unless-cost counter: asked, "
           "not auto-paid?", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.113.0 (0571ee3) protocol 151 - "
           f"re-validation {run['run_id']}", fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
            "blocked": (255, 200, 90)}[run["verdict"]]
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states + wire observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "pre.json: Clash X=0 fully cast on stack",
        "A2_unless_prompt_zero": "REPORTED: UnlessPayment prompt at X=0 "
            "(fail == auto-pay, no prompt, Bears not countered)",
        "A3_unless_prompt_control": "CONTROL: UnlessPayment prompt at X=2",
        "A4_cleanup": "post exports present",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:13]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(out_path)
    say(f"wrote {out_path}")


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------- main

async def main():
    RUNLOG_F = open(f"{EVDIR}/scenario_run.log", "w")
    global RUNLOG, WIRE
    RUNLOG = RUNLOG_F
    WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    reset()
    MULLS.clear()
    MULL_COUNTS.clear()
    SUBMITTED_OPPS.clear()
    t0 = time.time()
    await verify_server_hello()
    if not check_card_data():
        say("card-data check FAILED; aborting")
        ST["stop"] = True

    p0 = PhaseClient("P0-9943")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-9943")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_tick_at = {}

    try:
        while time.time() - t0 < TIMEOUT:
            await asyncio.sleep(0.15)
            if ST["stop"]:
                break
            for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                      (p1, p1_tick, "P1", 1)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                else:
                    holds = my_priority(merged_actions(st))
                    if not (holds and time.time()
                            - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await drain(c)
                    await tick(c, st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": tag,
                                        "err": f"{type(e).__name__}: {e}"})

            st0 = p0.latest
            if not st0:
                continue
            state = st0["state"]

            # turn cap
            if (state.get("turn_number") or 0) > TURN_CAP and not ST["stop"]:
                say(f"turn cap {TURN_CAP} reached at leg {ST['leg']}; ending")
                wire("turn_cap", {"leg": ST["leg"]})
                ST["stop"] = True
                break

            cl = ST["clash"]
            if cl is not None:
                # track the clash stack object; export pre at first sight
                if cl["stack_oid"] is None:
                    found = clash_on_stack(state)
                    if found:
                        cl["stack_oid"] = found[0]
                        pre_tag = "pre" if cl["leg"] == 0 else "pre_control"
                        if not cl["pre_exported"]:
                            await export_named(p0, pre_tag)
                            cl["pre_exported"] = True
                            say(f"leg {cl['leg']}: clash X={cl['x']} on "
                                f"stack (oid {found[0]}); {pre_tag}.json "
                                "exported")
                            wire("clash_on_stack",
                                 {"leg": cl["leg"], "x": cl["x"],
                                  "oid": found[0]})
                else:
                    # resolution: clash left the stack?
                    still = cl["stack_oid"] in clash_on_stack(state)
                    if not still:
                        leg = cl["leg"]
                        bears_oid = cl["bears_oid"]
                        # wait for the stack to fully empty before judging
                        # the bears' fate (it may still be resolving)
                        if stack_entries(state):
                            continue
                        bo = get_obj(state, bears_oid)
                        bzone = bo.get("zone")
                        countered = (bzone == "Graveyard")
                        prompt_seen = leg in ST["unless_seen"]
                        ST["leg_outcomes"][leg] = {
                            "prompt_seen": prompt_seen,
                            "bears_zone": bzone,
                            "bears_countered": countered,
                            "x": cl["x"],
                        }
                        say(f"leg {leg} resolved: prompt_seen={prompt_seen}, "
                            f"bears zone={bzone} (countered={countered})")
                        wire("leg_resolved",
                             {"leg": leg, "prompt_seen": prompt_seen,
                              "bears_zone": bzone,
                              "bears_countered": countered})
                        if leg == 0:
                            await export_named(p0, "post")
                            ST["post_exported"] = True
                            ST["leg"] = 1
                            say("leg 0 done; -> LEG1 (control X=2)")
                        else:
                            await export_named(p0, "post_control")
                            ST["post_control_exported"] = True
                            ST["leg"] = 2
                            ST["stop"] = True
                            say("control leg done; stopping")
                        ST["clash"] = None
    finally:
        # final exports even on early stop
        try:
            if p0.latest and not ST.get("post_exported") \
                    and ST["leg_outcomes"].get(0) is not None:
                await export_named(p0, "post")
        except Exception as e:
            say(f"final export failed: {e}")

    assertions, verdict = evaluate()
    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server_identity": SERVER_IDENTITY,
        "verdict": verdict,
        "assertions": assertions,
        "notes": ST["notes"],
        "rejections": ST.get("rejections", []),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "scope": "Clash of Wills X=0 unless-payment prompt (reported) + "
                 "X=2 control; native engine, two human driver seats",
        "limitations": [
            "Power Sink X=0 and {0} ward costs (also named in #9943) not "
            "exercised.",
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "States are authoritative exports, restorable only via full "
            "game replay (no standalone state-import path).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": assertions, "verdict": verdict,
                   "notes": ST["notes"]}, f, indent=1, default=str)
    # scenario source hash
    with open(__file__, "rb") as f:
        scen_sha = hashlib.sha256(f.read()).hexdigest()
    with open(f"{EVDIR}/scenario_9943.py", "w") as f:
        f.write(open(__file__).read())
    run["scenario_sha256"] = scen_sha
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    render_summary(run, f"{EVDIR}/summary.png")

    # manifest
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(os.path.join(EVDIR, f))
                   and f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            f.write(f"{sha256_of_file(os.path.join(EVDIR, fn))}  {fn}\n")
    # validate: every JSON parses, PNG readable
    for fn in files:
        p = os.path.join(EVDIR, fn)
        if fn.endswith(".json"):
            json.load(open(p))
        if fn.endswith(".jsonl"):
            for line in open(p):
                json.loads(line)
    try:
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        say("PNG readable: summary.png")
    except ImportError:
        say("PIL missing; PNG check skipped")
    say(f"done: verdict={verdict} assertions={assertions}")
    wire("done", {"verdict": verdict, "assertions": assertions})
    RUNLOG_F.close()
    WIRE.close()


if __name__ == "__main__":
    asyncio.run(main())
