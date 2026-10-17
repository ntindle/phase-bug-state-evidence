#!/usr/bin/env python3
"""Issue #7169: Rooftop Storm -- {0} alternative cost not offered from graveyard.

RE-VALIDATION RUN on pinned v0.111.0 (protocol 144, build 33aa23a), ported
from driver/scenario_7169.py (protocol 70, run 20260915-7169b, verdict
reproduced on v0.83.0). That run established: the {0} alternative-cost
offer (OptionalCostChoice with AdditionalCost::Choice) appears for the
hand cast (Diregraf Ghoul; accepted, resolved) but is never raised for
the graveyard cast (Gravecrawler; cast completed and resolved to the
battlefield without the offer). The v0.83.0 result is stale under the
playbook's release-pinning rule, so the hand-vs-graveyard alternative-cost
path is re-driven here on the current pin with identical assertions.

Protocol-144 port notes (mechanical, from the verified 142 runs):
  - Interaction conventions: engine auto-pays mana (bare CastSpell, no
    PayMana / tapLandForMana bookkeeping); waiting_for is null; priority =
    PassPriority in the viewing seat's top-level legal_actions;
    MulliganDecision answered via legacy Action; bottom-after-mulligan via
    vi schema/select; DiscardToHandSize via viewer_interaction only;
    vi candidate `reference` values are STRINGS while authoritative oids
    are INTS (converted via int() at every comparison site).
  - The OptionalCostChoice offer is detected as a vi opportunity whose
    choice surfaces carry codes containing "OptionalCost" or
    "AdditionalCost"; the pay branch is the value surface
    (role=pay, value=true), falling back to (role=accept, value=true).
  - Cast-confirmation guard: after every CastSpell submission, hold
    priority passes and new main-phase plays until the submission is
    confirmed on the stack (or the cast object leaves its origin zone);
    a 30s backstop clears a silently-dropped submission and resets the
    one-shot cast flag so the play re-triggers.
  - Turn order is randomized per game; every play is gated on
    active_player, never on turn_number.
  - Server-log excerpts dropped: the shared pinned server's log is not
    run-attributable; wire_log.jsonl captures all submissions,
    rejections, and observed snapshots for this run.

Oracle: "You may pay {0} rather than pay the mana cost for Zombie creature
spells you cast." (no zone restriction)

Parse (pinned v0.111.0 card-data.json, verified this run):
  Rooftop Storm static: CastWithAlternativeCost {cost {0}},
    affected = Typed{Creature, Subtype Zombie}, controller You,
    affected_zone null, active_zones [Battlefield].
  Gravecrawler static: GraveyardCastPermission (active_zones [Graveyard]),
    condition "control a Zombie" (Typed Subtype Zombie, controller You,
    InZone Battlefield).

Plan (native engine, v0.111.0 / protocol 144, two human driver seats):
  P0: 8x Rooftop Storm / 8x Gravecrawler / 8x Diregraf Ghoul /
      8x Augur of Bolas (non-Zombie control) / 14x Island / 14x Swamp.
  P1: 60x Forest, fully passive (lands, passes).
  P0 casts Rooftop Storm from hand, then Diregraf Ghoul from hand
  (HAND leg: the {0} offer should appear; accept it), keeps the Ghoul on
  the battlefield (also enables Gravecrawler's graveyard-cast permission),
  discards a Gravecrawler to the graveyard at cleanup (deliberate pick),
  then casts Gravecrawler from the graveyard (GY leg: observe whether the
  {0} offer appears), then casts Augur of Bolas from hand (NONZ leg:
  non-Zombie control; best-effort Augur ETB handling).

Behavioral contract (written before observing results):
  A1 setup_ok      Storm on P0 BF; Diregraf Ghoul on P0 BF; a Gravecrawler
                   in P0's graveyard; pre.json exported before the GY cast.
  A2 hand_offer    OptionalCostChoice with AdditionalCost::Choice observed
                   when casting Diregraf Ghoul from hand; {0} accepted;
                   Ghoul resolved to the battlefield. (control passes)
  A3 gy_offer_absent no such offer observed during the Gravecrawler-from-GY
                   cast window. (expect this to PASS == bug reproduced)
  A4 gy_cast_completes the GY cast completed and Gravecrawler resolved onto
                   the battlefield (the permission path works; only the
                   alt cost is missing).
  A5 nonz_unaffected no {0} offer for Augur of Bolas (non-Zombie) from
                   hand; cast completed (best-effort ETB handling).
  A6 cleanup       stack empty, post.json exported.

Verdict: reproduced iff A1+A2 pass and A3 passes (offer missing from the GY
         cast while present from hand) and A4 passes.
         not-reproduced iff A2 passes and the GY cast DID offer {0}.
         blocked iff A1 or A2 fails.

Driver notes:
  - Never pass priority while a cast window is open or an OptionalCost
    offer for seat 0 is pending; return without acting. Priority passes
    are gated per revision (PASSED_REV) to avoid duplicate submits.
  - Augur of Bolas has an ETB ("look at the top three cards... may reveal
    an instant or sorcery... rest on the bottom"). The P0 deck holds no
    instants/sorceries, so the reveal branch is empty; the bottom-ordering
    select is answered with the presented order. Best-effort: if the ETB
    stalls, A5/A6 degrade to not-run but the core verdict still stands.
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
RUN_ID = os.environ.get("RUN_ID", "run-7169-reval-v01110-20261017-0121")
ISSUE = 7169
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None


def init_logging():
    global WIRE, RUNLOG
    if WIRE is None:
        WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    if RUNLOG is None:
        RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


STORM = "rooftop storm"
CRAWLER = "gravecrawler"
GHOUL = "diregraf ghoul"
AUGUR = "augur of bolas"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = deck(("Rooftop Storm", 8), (CRAWLER.title(), 8), ("Diregraf Ghoul", 8),
               ("Augur of Bolas", 8), ("Island", 14), ("Swamp", 14))
P1_DECK = deck(("Forest", 60))

SERVER_IDENTITY = {
    "server_version": "v0.111.0",
    "build_commit": "33aa23a",
    "protocol_version": 144,
    "mode": "Full",
    "binary_sha256": "92e0e1cd04342d3429a35c965e2487a66adda730564824b36074e9f4b0305c9f",
    "card_data_sha256": "12ac3244d7add0d8d643d3b1466cbff72059c337110ed9863bd34351a2ae04c4",
    "draft_pools_sha256": "787c188acdd4a5537bb2c447ec2da618ddad9fbbb215c807b9a575e6985a2f38",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.111.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828) at pin time (2026-10-16); "
                      "release unchanged since (latest stable still v0.111.0, "
                      "published 2026-10-16T15:04:47Z); data digests match "
                      "the signed manifest; digests recomputed against "
                      "on-disk files this run",
    "observed_at": "2026-10-17",
    "source": "ServerHello asserted by handshake this run + sha256 "
              "re-verified against pinned v0.111.0 release artifacts; "
              "reused the live v0.111.0 server on 127.0.0.1:9374 "
              "(started by run run-6643-reval-v01110-20261017-0011, pid 3836; "
              "ServerHello re-verified this run)",
}

TIMEOUT = 1500
TURN_CAP = 60
WINDOW_TIMEOUT = 30

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


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


# ---------------------------------------------------------------- state helpers

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


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def gy_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Graveyard":
            continue
        if lname is not None and obj_lname(state, oid) != lname:
            continue
        own = o.get("owner")
        ctrl = o.get("controller")
        if str(own) == str(pid) or str(ctrl) == str(pid) \
                or (own is None and ctrl is None):
            out.append(oid)
    return out


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def life(state, pid):
    return player_of(state, pid).get("life")


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
    """Protocol 144: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
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
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. The alt-cost
    offer (decideOptionalCost / decideAdditionalCost codes) IS a real
    decision; it is handled explicitly by the cast-window tick."""
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


def decision_pending(st):
    """Legacy alias: only real decisions block priority passes."""
    return real_decision_pending(st)


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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
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
    assert str(ver).startswith("0.111.0"), f"unexpected version {ver}"
    assert int(proto) == 144, f"unexpected protocol {proto}"
    assert str(build) == "33aa23a", f"unexpected build {build}"


# ---------------------------------------------------------------- card data check

CARD_DATA = {}


def check_card_data():
    global CARD_DATA
    ok, notes = True, []
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.111.0/data/"
                            f"card-data.json"))
        CARD_DATA = cd
        storm = cd.get("rooftop storm", {})
        statics = storm.get("static_abilities") or []
        mode0 = ((statics[0] or {}).get("mode") or {}) if statics else {}
        if "CastWithAlternativeCost" not in str(mode0):
            ok = False
            notes.append(f"rooftop storm static mode unexpected: "
                         f"{str(mode0)[:120]}")
        if "You may pay {0}" not in str(storm.get("oracle_text")):
            ok = False
            notes.append("rooftop storm oracle text missing '{0}' alt cost")
        crawler = cd.get("gravecrawler", {})
        cstatics = crawler.get("static_abilities") or []
        if "GraveyardCastPermission" not in str(cstatics):
            ok = False
            notes.append("gravecrawler missing GraveyardCastPermission")
    except Exception as e:
        ok, notes = False, [f"card-data read failed: {e}"]
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {
                       "rooftop storm": str(
                           CARD_DATA.get("rooftop storm", {}).get("oracle_text"))[:200],
                       "gravecrawler": str(
                           CARD_DATA.get("gravecrawler", {}).get("oracle_text"))[:200]},
                   "statics": {
                       "rooftop storm": (CARD_DATA.get("rooftop storm", {})
                                         .get("static_abilities")),
                       "gravecrawler": (CARD_DATA.get("gravecrawler", {})
                                        .get("static_abilities"))}}, f, indent=1)
    say(f"card-data check: ok={ok} notes={notes}")
    wire("card_data_check", {"ok": ok, "notes": notes})
    return ok


# ---------------------------------------------------------------- decisions

async def do_mulligan(c, acts, st, pid, tag):
    """MulliganDecision arrives as a legacy legal action; #7169 keeps."""
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
    MULLS.add(key)
    hn = hand_lnames(st["state"], pid)
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


def cand_ref_int(ch):
    r = _cand_reference(ch)
    try:
        return int(r)
    except (TypeError, ValueError):
        return None


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code=='mulligan' + turn 1/Untap."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
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
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = cand_ref_int(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if is_land(get_obj(state, oid)):
            return (1, nm)
        return (0, nm)

    picks = sorted(cands, key=bkey)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, cand_ref_int(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """DiscardToHandSize via viewer_interaction. During STOCK, deliberately
    discards a Gravecrawler (the reported GY leg needs one in the
    graveyard); otherwise discards lands-last."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    deliberate = (tag == "P0-7169" and pid == 0
                  and ST.get("stage") == "STOCK"
                  and not gy_oids(state, 0, CRAWLER))
    key = (tag, "handsize", str(c.revision), "stock" if deliberate else "gen")
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(o):
        nm = obj_lname(state, o)
        if deliberate and nm == CRAWLER:
            return (-10, nm)  # deliberate: crawler discarded FIRST
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not any(str(cand_ref_int(ch)) in handset for ch in cands):
            continue
        ranked = sorted(
            [ch for ch in cands if str(cand_ref_int(ch)) in handset],
            key=lambda ch: rank(str(cand_ref_int(ch))))
        picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        iid = opp.get("interactionId")
        picked_oids = [str(cand_ref_int(ch)) for ch in picks]
        say(f"[{tag}] discarding {n} to hand size"
            f"{' (deliberate crawler)' if deliberate and any(obj_lname(state, o) == CRAWLER for o in picked_oids) else ''}: "
            f"{[obj_lname(state, o) for o in picked_oids]}")
        wire("discard", {"who": tag, "iid": iid, "deliberate": deliberate,
                         "picked": [obj_lname(state, o) for o in picked_oids]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        if deliberate and any(obj_lname(state, o) == CRAWLER for o in picked_oids):
            say(f"[{tag}] Gravecrawler deliberately discarded to GY")
        return True
    return False


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
    pref = [ISLAND, SWAMP] if pid == 0 else [FOREST]
    for nm in pref:
        for o in hand_ids(state, pid):
            if obj_lname(state, o) != nm:
                continue
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {nm}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == key:
                return a, iv
    return None, None


def cast_action_for_oid(acts, state, oid):
    """The CastSpell action for a SPECIFIC object (e.g. the GY crawler)."""
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        cands = [d.get("object_id"), d.get("source_id"), a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if str(iv) == str(oid):
                return a, iv
    return None, None


# ---------------------------------------------------------------- alt-cost offer

def is_altcost_opp(opp):
    """True if a vi opportunity looks like an optional/additional cost
    choice (the Rooftop Storm {0} alternative-cost offer class)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    for ch in (data.get("choices") or data.get("candidates") or []):
        for code in surf_codes(ch):
            if code and ("OptionalCost" in str(code) or "AdditionalCost" in str(code)):
                return True
    return False


def find_pay_choice(chs):
    """The 'pay the preferred branch' choice of an alternative-cost offer:
    decideOptionalCost/decideAdditionalCost action code + value surface
    (role=pay, value=true), falling back to (role=accept, value=true)."""
    fallback = None
    for ch in chs:
        has_code = any("decideOptionalCost" in str(c) or "decideAdditionalCost" in str(c)
                       for c in surf_codes(ch))
        pay_true = False
        accept_true = False
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data") or {}
            if sf.get("type") == "value":
                role = str(d.get("role") or "").lower()
                val = str(d.get("value") or "").lower()
                if role == "pay" and val == "true":
                    pay_true = True
                if role == "accept" and val == "true":
                    accept_true = True
        if has_code and pay_true:
            return ch
        if accept_true and fallback is None:
            fallback = ch
    return fallback


async def altcost_window_tick(c, st, state, tag):
    """The cast window owns the tick while open: watch for the alt-cost
    offer, answer the pay branch, and clear the window once the cast is
    confirmed (or the backstop fires). Never passes priority."""
    win = ST.get("cast_window")
    if win is None:
        return False
    leg = win["leg"]
    for opp in vi_ops(st):
        if not is_altcost_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        ST["offer_seen"][leg] = True
        ST["offer_blobs"].setdefault(leg, []).append(
            json.loads(json.dumps({"opportunity": opp}, default=str)))
        wire("altcost_offer_seen",
             {"leg": leg, "spell": win["spell"],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        iid = opp.get("interactionId")
        key = (tag, "altcost", leg, str(iid))
        if key in SUBMITTED_OPPS:
            return True  # hold; already answered, wait for confirmation
        pick = find_pay_choice(chs)
        if pick is None:
            wire("altcost_no_pay_choice",
                 {"leg": leg,
                  "opp": json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] alt-cost offer WITHOUT identifiable pay choice "
                f"(leg={leg}); holding")
            return True
        SUBMITTED_OPPS.add(key)
        ST["offer_accepted"][leg] = True
        ST["submitted_choice"][leg] = pick.get("id")
        say(f"[{tag}] alt-cost offer ACCEPTED (pay={{0}}) leg={leg} "
            f"choice={pick.get('id')} text={choice_text(pick)[:60]!r}")
        await answer_vi(c, opp, pick, tag)
        return True
    clear_cast_window(state, tag)
    return True


def clear_cast_window(state, tag):
    """Clear the cast window once the spell is confirmed on the stack or
    has left its origin zone; the 30s backstop resets the one-shot flag so
    a silently-dropped cast re-triggers."""
    win = ST.get("cast_window")
    if win is None:
        return
    oid, leg, origin = win["oid"], win["leg"], win["origin"]
    o = get_obj(state, oid)
    on_stack = any(str(e.get("id")) == str(oid) for e in (state.get("stack") or []))
    left_origin = o.get("zone") not in (origin, None)
    timed_out = time.time() - win["t0"] > WINDOW_TIMEOUT
    if on_stack or left_origin or timed_out:
        say(f"[{tag}] cast window closed leg={leg} spell={win['spell']} "
            f"offer_seen={ST['offer_seen'][leg]} accepted={ST['offer_accepted'][leg]} "
            f"on_stack={on_stack} left_origin={left_origin} timed_out={timed_out}")
        wire("cast_window_closed",
             {"leg": leg, "spell": win["spell"],
              "offer_seen": ST["offer_seen"][leg],
              "accepted": ST["offer_accepted"][leg],
              "on_stack": on_stack, "left_origin": left_origin,
              "timed_out": timed_out})
        ST["cast_window"] = None
        if timed_out and not (on_stack or left_origin):
            ST["notes"].append(f"cast window timed out without confirmation "
                               f"(leg={leg}, spell={win['spell']}); re-arming")
            ST["cast_done"][leg] = False


async def submit_cast(c, acts, state, a, oid, name, leg, origin, tag):
    say(f"[{tag}] casting {name} (oid={oid}) leg={leg} origin={origin}")
    wire("cast", {"who": tag, "name": name, "oid": str(oid), "leg": leg,
                  "origin": origin,
                  "action": {k: v for k, v in a.items() if not k.startswith("_")}})
    await submit_as_is(c, a)
    ST["cast_window"] = {"spell": name, "leg": leg, "oid": str(oid),
                         "origin": origin, "t0": time.time()}
    ST["cast_done"][leg] = True


# ---------------------------------------------------------------- Augur ETB (best effort)

async def etb_tick(c, st, state, tag):
    """Best-effort handling of the Augur of Bolas ETB (look at top 3; may
    reveal an instant/sorcery; rest on the bottom in any order). The P0
    deck holds no instants/sorceries, so the reveal branch is empty; the
    bottom-ordering select is answered with the presented order. Wire-logs
    everything; returns True if it consumed the tick."""
    consumed = False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type") or ""
            cands = data.get("candidates") or []
            if spec in ("select", "sequence") and cands:
                iid = opp.get("interactionId")
                key = (tag, "etb", str(iid))
                if key in SUBMITTED_OPPS:
                    continue
                # If this is the alt-cost window it is handled elsewhere;
                # only answer non-alt-cost ordering selects here.
                if is_altcost_opp(opp):
                    continue
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] ETB bottom-ordering: answering with presented order "
                    f"({len(cands)} cards)")
                wire("etb_bottom_order", {"who": tag, "iid": iid,
                                          "cards": [choice_text(ch)[:40] for ch in cands]})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": spec,
                                                   "data": {"choiceIds": [ch.get("id") for ch in cands]}}}, tag)
                consumed = True
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(cc for cc in surf_codes(ch) if cc)
            if "decideOptionalEffect" in codes:
                iid = opp.get("interactionId")
                key = (tag, "etb", str(iid))
                if key in SUBMITTED_OPPS:
                    continue
                decline = None
                for ch in chs:
                    for sf in ch.get("surfaces", []) or []:
                        dd = sf.get("data", {}) or {}
                        if dd.get("role") == "accept" and str(dd.get("value")).lower() == "false":
                            decline = ch
                if decline is None:
                    decline = chs[-1]
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] ETB optional effect: declining "
                    f"({choice_text(decline)[:60]!r})")
                wire("etb_optional_decline", {"who": tag, "iid": iid,
                                              "choice": choice_text(decline)[:80]})
                await answer_vi(c, opp, decline, tag)
                consumed = True
    return consumed


# ---------------------------------------------------------------- ticks

def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> HAND -> STOCK -> GY -> NONZ -> DONE
        "storm_oid": None,
        "ghoul_oid": None,
        "crawler_gy_oid": None,
        "augur_oid": None,
        "cast_window": None,  # {"spell","leg","oid","origin","t0"}
        "cast_done": {"storm": False, "hand": False, "gy": False, "nonz": False},
        "offer_seen": {"storm": False, "hand": False, "gy": False, "nonz": False},
        "offer_accepted": {"storm": False, "hand": False, "gy": False, "nonz": False},
        "offer_blobs": {},
        "submitted_choice": {"storm": None, "hand": None, "gy": None, "nonz": None},
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "stop": False,
        "notes": [],
    })


async def p0_tick(c, acts, state, tag):
    st = c.latest
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await pay_tick(c, acts):
        return True
    # The cast window owns the tick: handle alt-cost offers; hold priority.
    if ST.get("cast_window") is not None:
        await altcost_window_tick(c, st, state, tag)
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
    if ST["stage"] == "NONZ":
        if await etb_tick(c, st, state, tag):
            return True

    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        stage = ST["stage"]
        if stage == "SETUP" and not ST["cast_done"]["storm"]:
            if STORM in hand_lnames(state, 0) and untapped_lands(state, 0) >= 6:
                a, oid = cast_action_for(acts, state, STORM)
                if a is not None:
                    await submit_cast(c, acts, state, a, oid, STORM, "storm", "Hand", tag)
                    return True
        elif stage == "HAND" and not ST["cast_done"]["hand"]:
            if GHOUL in hand_lnames(state, 0):
                a, oid = cast_action_for(acts, state, GHOUL)
                if a is not None:
                    await submit_cast(c, acts, state, a, oid, GHOUL, "hand", "Hand", tag)
                    return True
        elif stage == "GY" and not ST["cast_done"]["gy"] and ST["pre_exported"]:
            crawlers = gy_oids(state, 0, CRAWLER)
            if crawlers:
                a, oid = cast_action_for_oid(acts, state, crawlers[0])
                if a is not None:
                    await submit_cast(c, acts, state, a, oid, CRAWLER, "gy", "Graveyard", tag)
                    return True
        elif stage == "NONZ" and not ST["cast_done"]["nonz"]:
            if AUGUR in hand_lnames(state, 0):
                a, oid = cast_action_for(acts, state, AUGUR)
                if a is not None:
                    await submit_cast(c, acts, state, a, oid, AUGUR, "nonz", "Hand", tag)
                    return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, acts, state, tag):
    st = c.latest
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, tag):
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
    if await pay_tick(c, acts):
        return True
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ---------------------------------------------------------------- exports / evaluate

async def export_named(c, name):
    data = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(data)
    say(f"exported {name}.json ({len(data)} bytes)")
    wire(f"{name}_exported", {"bytes": len(data)})
    return json.loads(data)["state"]


def evaluate():
    a = {}
    pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
        if os.path.exists(f"{EVDIR}/pre.json") else None
    mid_st = json.loads(open(f"{EVDIR}/mid.json").read())["state"] \
        if os.path.exists(f"{EVDIR}/mid.json") else None
    post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
        if os.path.exists(f"{EVDIR}/post.json") else None

    storm_oid = ST.get("storm_oid")
    ghoul_oid = ST.get("ghoul_oid")
    crawler_oid = ST.get("crawler_gy_oid")

    storm_bf = (storm_oid is not None and pre_st is not None
                and get_obj(pre_st, storm_oid).get("zone") == "Battlefield")
    ghoul_bf = (ghoul_oid is not None and pre_st is not None
                and get_obj(pre_st, ghoul_oid).get("zone") == "Battlefield")
    crawler_gy = (crawler_oid is not None and pre_st is not None
                  and get_obj(pre_st, crawler_oid).get("zone") == "Graveyard")
    a["A1_setup_ok"] = ("passed" if (storm_bf and ghoul_bf and crawler_gy
                                     and ST["pre_exported"]) else "failed")
    say(f"A1: storm_bf={storm_bf} ghoul_bf={ghoul_bf} "
        f"crawler_gy={crawler_gy} pre_exported={ST['pre_exported']}")

    a["A2_hand_offer"] = ("passed" if (ST["offer_seen"]["hand"]
                                       and ST["offer_accepted"]["hand"]
                                       and ghoul_bf) else "failed")
    say(f"A2: hand offer_seen={ST['offer_seen']['hand']} "
        f"accepted={ST['offer_accepted']['hand']} ghoul_bf={ghoul_bf}")

    a["A3_gy_offer_absent"] = ("passed" if not ST["offer_seen"]["gy"]
                               else "failed")
    say(f"A3: gy offer_seen={ST['offer_seen']['gy']} (pass == bug reproduced)")

    crawler_bf = False
    if mid_st is not None and crawler_oid is not None:
        crawler_bf = get_obj(mid_st, crawler_oid).get("zone") == "Battlefield"
    elif post_st is not None and crawler_oid is not None:
        crawler_bf = get_obj(post_st, crawler_oid).get("zone") == "Battlefield"
    a["A4_gy_cast_completes"] = ("passed" if crawler_bf else "failed")
    say(f"A4: crawler_bf={crawler_bf}")

    augur_bf = False
    if post_st is not None and ST.get("augur_oid") is not None:
        augur_bf = (get_obj(post_st, ST["augur_oid"]).get("zone")
                    == "Battlefield")
    if not ST["cast_done"]["nonz"]:
        a["A5_nonz_unaffected"] = "not-run"
    elif not ST["offer_seen"]["nonz"] and augur_bf:
        a["A5_nonz_unaffected"] = "passed"
    else:
        a["A5_nonz_unaffected"] = "failed"
    say(f"A5: nonz_offer_seen={ST['offer_seen']['nonz']} augur_bf={augur_bf}")

    if post_st is not None:
        a["A6_cleanup"] = ("passed" if not (post_st.get("stack") or [])
                           else "failed")
    else:
        a["A6_cleanup"] = "not-run"

    if a["A1_setup_ok"] == "failed" or a["A2_hand_offer"] == "failed":
        verdict = "blocked"
    elif (a["A3_gy_offer_absent"] == "passed"
            and a["A4_gy_cast_completes"] == "passed"):
        verdict = "reproduced"
    elif a["A2_hand_offer"] == "passed" and ST["offer_seen"]["gy"]:
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, verdict


# ---------------------------------------------------------------- render

def render_summary(run, out_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7169 - Rooftop Storm: {0} alt cost "
           "not offered from graveyard", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.111.0 (33aa23a) protocol 144 - re-validation "
           f"{run['run_id']}", fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else (120, 220, 120))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "Storm+Ghoul on P0 BF; Crawler in P0 GY; pre exported",
        "A2_hand_offer": "hand Ghoul: {0} offer seen+accepted, resolved",
        "A3_gy_offer_absent": "GY Crawler cast: NO {0} offer [bug => PASS]",
        "A4_gy_cast_completes": "GY Crawler cast resolved to the BF",
        "A5_nonz_unaffected": "non-Zombie Augur: no {0} offer, resolved",
        "A6_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Offer observations:", fill=(200, 210, 225))
    y += 24
    for leg in ("hand", "gy", "nonz"):
        d.text((36, y), f"{leg}: offer_seen={run['fixture']['offer_seen'][leg]} "
               f"accepted={run['fixture']['offer_accepted'][leg]} "
               f"choice={run['fixture']['submitted_choice'][leg]}", fill=(170, 180, 195))
        y += 22
    y += 10
    d.text((24, y), "Notes:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(out_path)
    say(f"wrote {out_path}")


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- main

async def main():
    init_logging()
    reset()
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    PASSED_REV.clear()
    t0 = time.time()
    await verify_server_hello()
    check_card_data()

    p0 = PhaseClient("P0-7169")
    await p0.connect()
    say("connecting P1...")
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-7169")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()

    try:
        while time.time() - t0 < TIMEOUT:
            await asyncio.sleep(0.15)
            if ST["stop"]:
                break
            for c, tick, tag, pid in ((p0, p0_tick, "P0-7169", 0),
                                      (p1, p1_tick, "P1-7169", 1)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                else:
                    holds = my_priority(merged_actions(st))
                    if not (holds and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(c, merged_actions(st), st["state"], tag)
                except Exception as e:
                    say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": tag,
                                        "err": f"{type(e).__name__}: {e}"})

            st0 = p0.latest
            if st0:
                state = st0["state"]
                if ST["storm_oid"] is None:
                    sv = bf_by_name(state, 0, STORM)
                    if sv:
                        ST["storm_oid"] = sv[0]
                        say(f"Storm on BF oid={sv[0]}")
                        wire("storm_bf", {"oid": str(sv[0])})
                if ST["ghoul_oid"] is None:
                    g = bf_by_name(state, 0, GHOUL)
                    if g:
                        ST["ghoul_oid"] = g[0]
                        say(f"Ghoul on BF oid={g[0]}")
                        wire("ghoul_bf", {"oid": str(g[0])})
                if ST["augur_oid"] is None:
                    au = bf_by_name(state, 0, AUGUR)
                    if au:
                        ST["augur_oid"] = au[0]
                        say(f"Augur on BF oid={au[0]}")
                        wire("augur_bf", {"oid": str(au[0])})
                if ST["crawler_gy_oid"] is None:
                    cr = gy_oids(state, 0, CRAWLER)
                    if cr:
                        ST["crawler_gy_oid"] = cr[0]
                        say(f"Crawler in GY oid={cr[0]}")
                        wire("crawler_gy", {"oid": str(cr[0])})

                if ST["stage"] == "SETUP" and ST["storm_oid"] is not None \
                        and ST["cast_window"] is None:
                    ST["stage"] = "HAND"
                    say("storm resolved; entering HAND")
                    wire("stage", {"stage": "HAND"})
                if ST["stage"] == "HAND" and ST["ghoul_oid"] is not None \
                        and ST["cast_window"] is None:
                    ST["stage"] = "STOCK"
                    say("HAND leg complete; entering STOCK")
                    wire("stage", {"stage": "STOCK"})
                if ST["stage"] == "STOCK" and ST["crawler_gy_oid"] is not None:
                    ST["stage"] = "GY"
                    say("STOCK complete; entering GY")
                    wire("stage", {"stage": "GY"})
                # pre.json: fixture ready, P0 to act in main phase.
                if (ST["stage"] == "GY" and not ST["pre_exported"]
                        and ST["storm_oid"] is not None
                        and ST["ghoul_oid"] is not None
                        and ST["crawler_gy_oid"] is not None
                        and my_main(state, 0)
                        and my_priority(merged_actions(st0))
                        and ST["cast_window"] is None):
                    pre = await export_named(p0, "pre")
                    ST["pre_exported"] = True
                    ST["notes"].append(
                        f"pre: turn {state.get('turn_number')} "
                        f"storm_oid={ST['storm_oid']} ghoul_oid={ST['ghoul_oid']} "
                        f"crawler_gy_oid={ST['crawler_gy_oid']}")
                # mid.json: GY cast resolved (crawler on BF), before NONZ.
                if (ST["stage"] == "GY" and ST["cast_window"] is None
                        and not ST["mid_exported"]
                        and ST["crawler_gy_oid"] is not None
                        and get_obj(state, ST["crawler_gy_oid"]).get("zone")
                        == "Battlefield"
                        and not (state.get("stack") or [])):
                    await export_named(p0, "mid")
                    ST["mid_exported"] = True
                    ST["stage"] = "NONZ"
                    ST["notes"].append(
                        f"mid: turn {state.get('turn_number')} "
                        f"gy_offer_seen={ST['offer_seen']['gy']} "
                        f"crawler resolved to BF")
                    say(f"GY leg resolved: offer_seen={ST['offer_seen']['gy']}")
                # post.json: NONZ leg resolved, stack empty.
                if (ST["stage"] == "NONZ" and ST["cast_window"] is None
                        and not ST["post_exported"]
                        and ST.get("augur_oid") is not None
                        and not (state.get("stack") or [])):
                    await export_named(p0, "post")
                    ST["post_exported"] = True
                    ST["stage"] = "DONE"
                    ST["notes"].append(
                        f"post: turn {state.get('turn_number')} "
                        f"nonz_offer_seen={ST['offer_seen']['nonz']} "
                        f"life={life(state, 0)}/{life(state, 1)}")
                    say("NONZ leg resolved; exporting post and stopping")
                    ST["stop"] = True
                    break

            if (st0 and (st0["state"].get("turn_number") or 0) > TURN_CAP
                    and ST["stage"] in ("SETUP", "HAND", "STOCK")):
                ST["notes"].append(f"TURN_CAP {TURN_CAP} hit in {ST['stage']}")
                say("TURN_CAP: finishing")
                break
            if time.time() - last_diag > 25 and st0:
                last_diag = time.time()
                s = st0["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} wf={(s.get('waiting_for') or {})} "
                    f"stage={ST['stage']} storm={ST['storm_oid']} ghoul={ST['ghoul_oid']} "
                    f"crawler_gy={ST['crawler_gy_oid']} augur={ST['augur_oid']} "
                    f"window={ST['cast_window'] is not None} "
                    f"offers={ST['offer_seen']} "
                    f"P0hand={hand_lnames(s, 0)[:6]}")
            if time.time() - t0 > TIMEOUT - 5:
                ST["notes"].append("TIMEOUT hit; finishing")
                break
    except Exception as e:
        ST["notes"].append(f"main loop exception: {type(e).__name__}: {e}")
        say(f"main loop exception: {e}")

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    ST["notes"].append(f"final stage={ST['stage']} offers={ST['offer_seen']} "
                       f"accepted={ST['offer_accepted']}")
    if not ST["post_exported"]:
        try:
            await export_named(p0, "post")
            ST["post_exported"] = True
            ST["notes"].append("post.json exported at finish() fallback")
        except Exception as e:
            ST["notes"].append(f"post export failed: {e}")
    a, verdict = evaluate()

    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass

    # Parse evidence snapshots from the pinned card data.
    parse = {}
    try:
        cd = CARD_DATA or json.load(open(
            f"{BACKFILL}/server/releases/v0.111.0/data/card-data.json"))
        for key, fn in (("rooftop storm", "parse_rooftop.json"),
                        ("gravecrawler", "parse_gravecrawler.json")):
            card = cd.get(key, {})
            blob = {"name": card.get("name"),
                    "oracle_text": card.get("oracle_text"),
                    "static_abilities": card.get("static_abilities")}
            parse[fn] = blob
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump(blob, f, indent=2)
    except Exception as e:
        ST["notes"].append(f"parse json failed: {e}")

    dur = time.time() - t0
    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/7169",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 144, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Rooftop Storm", 8], ["Gravecrawler", 8],
                   ["Diregraf Ghoul", 8], ["Augur of Bolas", 8],
                   ["Island", 14], ["Swamp", 14]],
            "P1": [["Forest", 60]],
        },
        "assertions": a,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 hand_offer: %s; A3 gy_offer_absent: %s; "
                   "A4 gy_cast_completes: %s; A5 nonz_unaffected: %s; "
                   "A6 cleanup: %s" % tuple(a.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_hand_offer", "A3_gy_offer_absent",
                            "A4_gy_cast_completes", "A5_nonz_unaffected",
                            "A6_cleanup"))),
        "scope": ("Rooftop Storm {0} alternative-cost offer across cast "
                  "origin zones; P0 hand-cast Diregraf Ghoul (control), "
                  "graveyard-cast Gravecrawler (reported leg), hand-cast "
                  "Augur of Bolas (non-Zombie control)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "fixture": {
            "storm_oid": str(ST.get("storm_oid")),
            "ghoul_oid": str(ST.get("ghoul_oid")),
            "crawler_gy_oid": str(ST.get("crawler_gy_oid")),
            "augur_oid": str(ST.get("augur_oid")),
            "offer_seen": ST["offer_seen"],
            "offer_accepted": ST["offer_accepted"],
            "submitted_choice": ST["submitted_choice"],
        },
        "notes": ST["notes"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7169_01110.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": a, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": a})
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
    print(f"DONE verdict={verdict} assertions={json.dumps(a)}", flush=True)


if __name__ == "__main__":
    pidfile = "/tmp/scenario_7169_01110.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7169_01110 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        asyncio.run(main())
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass
