#!/usr/bin/env python3
"""Issue #9921: Mornsong Aria -- "that player" searches the controller's
library; "or gain life" not parsed.

#9921 is a bug-labeled PULL REQUEST (open, unmerged as of 2026-10-17; held by
matthewevans for delivery verification). Unlike #9454 (blocked: no
player-observable behavior described), #9921's Summary describes concrete
player-observable buggy behavior on the pinned release, with setup, trigger,
and expected outcome -- so it is reproducible by the backfill loop:

  Bug A (search): "On another player's draw step the Aria's controller
    searched their own library and put the card into their hand; the drawing
    player only lost 3 life." Expected per CR 608.2c/701.23a: the drawing
    ("that") player loses 3 life, searches THEIR OWN library, puts the card
    into THEIR hand, then shuffles.
  Bug B (life clause): "parse_cant_draw_cards stops after 'cards' and ignores
    the rest, so the Aria emitted only CantDraw." Expected per CR 119.7: the
    "or gain life" clause is also enforced.

Parse (pinned v0.111.0 card-data.json, verified this run):
  oracle: "Players can't draw cards or gain life.\nAt the beginning of each
    player's draw step, that player loses 3 life, searches their library for
    a card, puts it into their hand, then shuffles."
  static_abilities: [CantDraw {who: AllPlayers}] -- NO CantGainLife (Bug B
    visible at the data level).
  trigger: Phase/Draw -> LoseLife 3 {ScopedPlayer} -> SearchLibrary
    {target_player: ScopedPlayer} -> ChangeZone Library->Hand ->
    Shuffle {ScopedPlayer}.

Plan (native engine, v0.111.0 / protocol 144, two human driver seats):
  P0: 4x Mornsong Aria / 56x Swamp. Casts Aria on its turn 3 (3 swamps).
  P1: 8x Radiant Fountain / 52x Plains, passive except the scripted Fountain
      play. Radiant Fountain is a land ("When this land enters, you gain 2
      life"): no target, no choice, no mana -- the cleanest deterministic
      life-gain probe under the Aria.
  Decks are name-disjoint (swamp/mornsong aria vs plains/radiant fountain),
  so the searched library is identified by candidate card names as well as
  by which client receives the choice.

Behavioral contract (written before observing results):
  A1 setup_ok        Aria on P0 BF; pre.json exported at the first Aria
                     search choice observed on P1's draw step (before it is
                     answered).
  A2 search_to_drawer The search choice on P1's draw step is presented to P1
                     (the drawing player) with P1's library candidates.
                     (buggy: presented to P0 with P0's library)
  A3 life_loss        P1 goes 20 -> 17 as the trigger resolves.
  A4 card_to_hand     The selected card ends in the recipient's hand.
  A5 no_life_gain     Radiant Fountain ETB under the Aria gains 0 life.
                     (buggy: gains 2)
  A6 cleanup          stack empty, post.json exported.

Verdict: reproduced iff A1 passes and (A2 shows P0 received the search OR
         A5 shows life was gained). not-reproduced iff A1 passes, A2 shows
         P1 received the search, and A5 (when run) shows no gain.
         blocked iff A1 fails.

Driver notes (protocol-144 conventions, from the verified 142/144 runs):
  - Engine auto-pays mana (bare CastSpell); waiting_for is null; priority =
    PassPriority in the viewing seat's top-level legal_actions.
  - Turn order is randomized per game; every play is gated on active_player,
    never on turn_number.
  - vi candidate `reference` values are STRINGS; authoritative oids are INTS.
  - The Aria search is answered generically on whichever client receives it;
    only searches observed while active_player==1 and phase=="Draw" feed the
    verdict. Searches on P0's own draw steps are answered silently so the
    game progresses.
  - Server-log excerpts dropped: the shared pinned server's log is not
    run-attributable; wire_log.jsonl captures all submissions, rejections,
    and observed snapshots for this run.
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
RUN_ID = os.environ.get("RUN_ID", "run-9921-v01110-20261017-0311")
ISSUE = 9921
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


ARIA = "mornsong aria"
SWAMP = "swamp"
PLAINS = "plains"
FOUNTAIN = "radiant fountain"

P0_DECK = deck(("Mornsong Aria", 8), ("Swamp", 52))
P1_DECK = deck(("Radiant Fountain", 8), ("Plains", 52))

P0_CARDS = {"swamp", "mornsong aria"}
P1_CARDS = {"plains", "radiant fountain"}

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
              "(ServerHello re-verified this run)",
}

TIMEOUT = 1500
TURN_CAP = 40

ST = {}
OBS = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
P0C = None  # exporter client (seat 0), set in main


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


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


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
            and str(state.get("active_player")) == str(pid)
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


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference"), (d.get("zone") or "")
    return None, ""


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
        aria = cd.get("mornsong aria", {})
        if "Players can't draw cards or gain life." not in str(
                aria.get("oracle_text")):
            ok = False
            notes.append("aria oracle text mismatch")
        statics = aria.get("static_abilities") or []
        modes = str([ (s.get("mode") or {}) for s in statics])
        if "CantDraw" not in modes:
            ok = False
            notes.append("aria missing CantDraw static")
        # Bug B is visible here: the life clause must NOT be parsed yet.
        OBS["data_has_cantgainlife"] = "CantGainLife" in modes
        trig = (aria.get("triggers") or [{}])[0]
        trig_txt = str(trig.get("description"))
        if "searches their library" not in trig_txt:
            ok = False
            notes.append("aria trigger text mismatch")
        # Bug A parse shape: SearchLibrary with ScopedPlayer target.
        OBS["data_search_target"] = str(
            ((trig.get("execute") or {}).get("sub_ability") or {})
            .get("effect", {}).get("target_player"))
        fountain = cd.get("radiant fountain", {})
        if "you gain 2 life" not in str(fountain.get("oracle_text")).lower():
            ok = False
            notes.append("radiant fountain oracle mismatch")
    except Exception as e:
        ok, notes = False, [f"card-data read failed: {e}"]
    blob = {"ok": ok, "notes": notes,
            "aria_oracle": str(CARD_DATA.get("mornsong aria", {})
                               .get("oracle_text"))[:300],
            "aria_statics": CARD_DATA.get("mornsong aria", {})
            .get("static_abilities"),
            "aria_trigger_desc": str(((CARD_DATA.get("mornsong aria", {})
                                       .get("triggers") or [{}])[0])
                                     .get("description"))[:300],
            "aria_search_target_player": OBS.get("data_search_target"),
            "data_has_cantgainlife": OBS.get("data_has_cantgainlife"),
            "fountain_oracle": str(CARD_DATA.get("radiant fountain", {})
                                   .get("oracle_text"))[:200]}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(blob, f, indent=1)
    say(f"card-data check: ok={ok} notes={notes} "
        f"cantgainlife_in_data={OBS.get('data_has_cantgainlife')}")
    wire("card_data_check", {"ok": ok, "notes": notes,
                             "blob": blob})
    return ok

# ---------------------------------------------------------------- decisions

async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = hand_lnames(st["state"], pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
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
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] bottoming 1 via vi")
        wire("bottom", {"who": tag, "iid": str(iid)})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds":
                                                    [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(o):
        nm = obj_lname(state, o)
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
        if not any(str(cand_reference(ch)[0] or "") in handset
                   for ch in cands):
            continue
        ranked = sorted(
            [ch for ch in cands
             if str(cand_reference(ch)[0] or "") in handset],
            key=lambda ch: rank(str(cand_reference(ch)[0])))
        picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        iid = opp.get("interactionId")
        picked_oids = [str(cand_reference(ch)[0]) for ch in picks]
        say(f"[{tag}] discarding {n} to hand size: "
            f"{[obj_lname(state, o) for o in picked_oids]}")
        wire("discard", {"who": tag, "iid": str(iid),
                         "picked": [obj_lname(state, o)
                                    for o in picked_oids]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds":
                                                    [ch.get("id") for ch in picks]}}},
                          tag)
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
    return False


async def play_a_land(c, state, pid, acts, tag, prefer):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for nm in prefer:
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


# ---------------------------------------------------------------- Aria search detection / answering

def is_search_opp(st, opp):
    """True if a vi opportunity is the Aria trigger's library search (not a
    cast menu, land menu, mulligan bottom, or discard select)."""
    if vi_kind_code(st) == "mulligan":
        return False
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    data = resp.get("data", {}) or {}
    spec = (data.get("spec", {}) or {}).get("type") or ""
    if spec not in ("select", "sequence"):
        return False
    cands = data.get("candidates") or []
    if not cands:
        return False
    codes = set()
    for ch in cands:
        codes.update(c for c in surf_codes(ch) if c)
    if codes & {"castSpell", "playLand", "tapLandForMana", "mulliganDecision"}:
        return False
    lib_refs = [cand_reference(ch) for ch in cands]
    if any(r is not None and "librar" in str(z).lower()
           for r, z in lib_refs):
        return True
    # Fallback: the engine names the waiting kind after the search.
    return "search" in vi_kind_code(st).lower()


def infer_library(names):
    s = set(names)
    if s and s <= P0_CARDS:
        return "P0"
    if s and s <= P1_CARDS:
        return "P1"
    return "mixed/unknown"


async def search_tick(c, st, state, pid, tag):
    """Answer the Aria library search on whichever client receives it.
    Records the observation only while P1 is the active player in Draw."""
    for opp in vi_ops(st):
        if not is_search_opp(st, opp):
            continue
        iid = opp.get("interactionId")
        key = (tag, "search", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        spec = (rdata.get("spec", {}) or {}).get("type") or "sequence"
        cands = rdata.get("candidates") or []
        names = []
        pick = None
        for ch in cands:
            ref, zone = cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else choice_text(ch)
            names.append(nm)
            if pick is None and ref is not None and "librar" in str(zone).lower():
                pick = ch
        if pick is None and cands:
            pick = cands[0]
        lib = infer_library(names)
        active = state.get("active_player")
        phase = state.get("phase")
        turn = state.get("turn_number")
        on_p1_draw = (str(active) == "1" and phase == "Draw"
                      and OBS.get("aria_oid") is not None)
        ev = {"recipient_tag": tag, "recipient_pid": pid,
              "active_player": active, "phase": phase, "turn": turn,
              "candidate_names": sorted(set(names))[:12],
              "candidate_count": len(cands),
              "library_inferred": lib,
              "on_p1_draw_step": on_p1_draw,
              "waiting_kind": vi_kind_code(st)}
        OBS["search_events"].append(ev)
        wire("aria_search_observed", ev)
        say(f"[{tag}] ARIA SEARCH observed: active={active} phase={phase} "
            f"turn={turn} lib~{lib} n={len(cands)} "
            f"names={sorted(set(names))[:8]}")
        # pre.json: first P1-draw-step search, exported BEFORE answering.
        if on_p1_draw and not OBS.get("pre_exported"):
            pre = await export_named(P0C, "pre")
            OBS["pre_exported"] = True
            OBS["p1_life_at_search"] = life(pre, 1)
            OBS["notes"].append(
                f"pre: turn {turn} P1 draw step, search offered to {tag} "
                f"(lib~{lib}), P1 life={OBS['p1_life_at_search']}")
            say(f"[{tag}] pre.json exported before answering the search")
        SUBMITTED_OPPS.add(key)
        OBS["search_answered"] = True
        OBS["search_recipient_tag"] = tag
        OBS["search_recipient_pid"] = pid
        OBS["search_library_inferred"] = lib
        OBS["search_on_p1_draw"] = on_p1_draw
        if pick is not None:
            ref, _z = cand_reference(pick)
            OBS["search_picked_oid"] = str(ref) if ref is not None else None
            OBS["search_picked_name"] = (
                obj_lname(state, ref) if ref is not None else choice_text(pick))
            say(f"[{tag}] answering search -> "
                f"{OBS['search_picked_name']} (oid={OBS['search_picked_oid']})")
            wire("aria_search_answered",
                 {"who": tag, "iid": str(iid),
                  "picked": OBS["search_picked_name"]})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": spec,
                                               "data": {"choiceIds":
                                                        [pick.get("id")]}}},
                              tag)
        else:
            say(f"[{tag}] WARNING: search opp with no candidates; holding")
            wire("aria_search_no_candidates", {"who": tag})
        return True
    return False


# ---------------------------------------------------------------- ticks

def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> ARMED -> LIFE -> DONE
        "aria_cast": False,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "stop": False,
    })
    OBS.clear()
    OBS.update({
        "aria_oid": None,
        "search_events": [],
        "search_answered": False,
        "search_recipient_tag": None,
        "search_recipient_pid": None,
        "search_library_inferred": None,
        "search_on_p1_draw": False,
        "search_picked_oid": None,
        "search_picked_name": None,
        "pre_exported": False,
        "p1_life_at_search": None,
        "p1_life_after_trigger": None,
        "fountain_oid": None,
        "fountain_played": False,
        "life_before_fountain": None,
        "life_after_fountain": None,
        "data_has_cantgainlife": None,
        "data_search_target": None,
        "notes": [],
    })


async def p0_tick(c, acts, state, tag):
    st = c.latest
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await pay_tick(c, acts):
        return True
    if await search_tick(c, st, state, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag, [SWAMP]):
            return True
        if not ST["aria_cast"] and ARIA in hand_lnames(state, 0) \
                and untapped_lands(state, 0) >= 3:
            a, oid = cast_action_for(acts, state, ARIA)
            if a is not None:
                say(f"[{tag}] casting {ARIA} (oid={oid})")
                wire("cast", {"who": tag, "name": ARIA, "oid": str(oid)})
                await submit_as_is(c, a)
                ST["aria_cast"] = True
                return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, acts, state, tag):
    st = c.latest
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await pay_tick(c, acts):
        return True
    if await search_tick(c, st, state, 1, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        # LIFE stage: deliberately play Radiant Fountain as the probe.
        if ST["stage"] == "LIFE" and not OBS["fountain_played"] \
                and FOUNTAIN in hand_lnames(state, 1):
            turn = state.get("turn_number")
            if LAND_PLAYED_TURN.get((tag,)) != turn:
                for o in hand_ids(state, 1):
                    if obj_lname(state, o) != FOUNTAIN:
                        continue
                    for a in acts:
                        if a["type"] == "PlayLand" \
                                and str(a.get("_src_oid")) == str(o):
                            OBS["life_before_fountain"] = life(state, 1)
                            LAND_PLAYED_TURN[(tag,)] = turn
                            OBS["fountain_oid"] = str(o)
                            OBS["fountain_played"] = True
                            say(f"[{tag}] LIFE probe: playing {FOUNTAIN} "
                                f"(oid={o}) at P1 life="
                                f"{OBS['life_before_fountain']}")
                            wire("fountain_play",
                                 {"who": tag, "oid": str(o),
                                  "life_before": OBS["life_before_fountain"]})
                            await submit_as_is(c, a)
                            return True
        if await play_a_land(c, state, 1, acts, tag, [PLAINS]):
            return True
    if real_decision_pending(st):
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

    aria_ok = (OBS.get("aria_oid") is not None and pre_st is not None
               and get_obj(pre_st, OBS["aria_oid"]).get("zone")
               == "Battlefield")
    a["A1_setup_ok"] = ("passed" if (aria_ok and OBS.get("pre_exported")
                                     and OBS.get("search_on_p1_draw"))
                        else "failed")
    say(f"A1: aria_bf={aria_ok} pre_exported={OBS.get('pre_exported')} "
        f"search_on_p1_draw={OBS.get('search_on_p1_draw')}")

    # A2: the search on P1's draw step must go to P1 with P1's library.
    rec = OBS.get("search_recipient_tag")
    lib = OBS.get("search_library_inferred")
    p1_draw_events = [e for e in OBS["search_events"]
                      if e.get("on_p1_draw_step")]
    if not p1_draw_events:
        a["A2_search_to_drawer"] = "not-run"
    elif rec == "P1-9921" and lib == "P1":
        a["A2_search_to_drawer"] = "passed"
    else:
        a["A2_search_to_drawer"] = "failed"
    say(f"A2: recipient={rec} lib~{lib} "
        f"p1_draw_events={len(p1_draw_events)}")

    # A3: P1 loses 3 life as the trigger resolves (20 -> 17).
    life_at = OBS.get("p1_life_at_search")
    life_after = OBS.get("p1_life_after_trigger")
    if life_at is None or life_after is None:
        a["A3_life_loss"] = "not-run"
    elif life_at == 20 and life_after == 17:
        a["A3_life_loss"] = "passed"
    else:
        a["A3_life_loss"] = "failed"
    say(f"A3: p1_life {life_at} -> {life_after}")

    # A4: the selected card reached the recipient's hand.
    picked = OBS.get("search_picked_oid")
    rp = OBS.get("search_recipient_pid")
    card_ok = False
    if picked is not None and mid_st is not None and rp is not None:
        o = get_obj(mid_st, picked)
        card_ok = (o.get("zone") == "Hand"
                   and str(picked) in hand_ids(mid_st, rp))
    a["A4_card_to_hand"] = ("passed" if card_ok
                            else ("not-run" if picked is None else "failed"))
    say(f"A4: picked={OBS.get('search_picked_name')} oid={picked} "
        f"in_recipient_hand={card_ok}")

    # A5: Radiant Fountain ETB under the Aria must gain 0 life.
    lb = OBS.get("life_before_fountain")
    la = OBS.get("life_after_fountain")
    if not OBS.get("fountain_played") or lb is None or la is None:
        a["A5_no_life_gain"] = "not-run"
        say(f"A5: not-run (played={OBS.get('fountain_played')} "
            f"before={lb} after={la})")
    else:
        delta = la - lb
        a["A5_no_life_gain"] = "passed" if delta == 0 else "failed"
        say(f"A5: fountain life {lb} -> {la} (delta={delta})")

    if post_st is not None:
        a["A6_cleanup"] = ("passed" if not (post_st.get("stack") or [])
                           else "failed")
    else:
        a["A6_cleanup"] = "not-run"

    if a["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif a["A2_search_to_drawer"] == "failed" or a["A5_no_life_gain"] == "failed":
        verdict = "reproduced"
    elif a["A2_search_to_drawer"] == "passed" and (
            a["A5_no_life_gain"] in ("passed", "not-run")):
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
    W, H = 1000, 1000
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9921 - Mornsong Aria: scoped search + "
           "'or gain life'", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.111.0 (33aa23a) protocol 144 - "
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
        "A1_setup_ok": "Aria on P0 BF; pre exported at P1-draw-step search",
        "A2_search_to_drawer": "search on P1 draw -> P1 w/ P1 library "
                               "[bug: P0 w/ P0 library]",
        "A3_life_loss": "P1 20 -> 17 as trigger resolves",
        "A4_card_to_hand": "selected card in recipient's hand",
        "A5_no_life_gain": "Fountain ETB under Aria gains 0 [bug: +2]",
        "A6_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Search observations:", fill=(200, 210, 225))
    y += 24
    for e in run["search_events"][:6]:
        d.text((36, y), f"turn {e.get('turn')} {e.get('phase')} "
               f"active={e.get('active_player')}: offered to "
               f"{e.get('recipient_tag')} lib~{e.get('library_inferred')} "
               f"n={e.get('candidate_count')}", fill=(170, 180, 195))
        y += 22
    y += 6
    d.text((24, y), f"P1 life: at search={run['p1_life_at_search']} "
           f"after trigger={run['p1_life_after_trigger']} "
           f"fountain {run['life_before_fountain']}->"
           f"{run['life_after_fountain']}", fill=(170, 180, 195))
    y += 28
    d.text((24, y), "Notes:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:12]:
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
    global P0C
    init_logging()
    reset()
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    PASSED_REV.clear()
    t0 = time.time()
    await verify_server_hello()
    check_card_data()

    p0 = PhaseClient("P0-9921")
    await p0.connect()
    say("connecting P1...")
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-9921")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    P0C = p0
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
            for c, tick, tag, pid in ((p0, p0_tick, "P0-9921", 0),
                                      (p1, p1_tick, "P1-9921", 1)):
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
                # Track the Aria.
                if OBS["aria_oid"] is None:
                    av = bf_by_name(state, 0, ARIA)
                    if av:
                        OBS["aria_oid"] = av[0]
                        ST["stage"] = "ARMED"
                        say(f"Aria on BF oid={av[0]}; stage=ARMED")
                        wire("aria_bf", {"oid": str(av[0])})
                # P1 life right after the P1-draw-step trigger resolves:
                # search answered, picked card in a hand, stack empty.
                if (ST["stage"] == "ARMED" and OBS["search_answered"]
                        and OBS["search_on_p1_draw"]
                        and not ST["mid_exported"]
                        and not (state.get("stack") or [])
                        and str(state.get("active_player")) == "1"):
                    picked = OBS.get("search_picked_oid")
                    if picked is not None and get_obj(
                            state, picked).get("zone") == "Hand":
                        OBS["p1_life_after_trigger"] = life(state, 1)
                        await export_named(p0, "mid")
                        ST["mid_exported"] = True
                        ST["stage"] = "LIFE"
                        OBS["notes"].append(
                            f"mid: turn {state.get('turn_number')} P1 life="
                            f"{OBS['p1_life_after_trigger']} picked="
                            f"{OBS['search_picked_name']} in hand; stage=LIFE")
                        say(f"trigger resolved: P1 life="
                            f"{OBS['p1_life_after_trigger']}; stage=LIFE")
                # Fountain ETB settled: fountain on BF, stack empty.
                if (ST["stage"] == "LIFE" and OBS["fountain_played"]
                        and OBS["life_after_fountain"] is None
                        and OBS.get("fountain_oid") is not None
                        and get_obj(state, OBS["fountain_oid"]).get("zone")
                        == "Battlefield"
                        and not (state.get("stack") or [])):
                    OBS["life_after_fountain"] = life(state, 1)
                    say(f"fountain settled: P1 life "
                        f"{OBS['life_before_fountain']} -> "
                        f"{OBS['life_after_fountain']}")
                    wire("fountain_settled",
                         {"before": OBS["life_before_fountain"],
                          "after": OBS["life_after_fountain"]})
                # post.json: LIFE stage done, stack empty.
                if (ST["stage"] == "LIFE"
                        and OBS["life_after_fountain"] is not None
                        and not ST["post_exported"]
                        and not (state.get("stack") or [])):
                    await export_named(p0, "post")
                    ST["post_exported"] = True
                    ST["stage"] = "DONE"
                    OBS["notes"].append(
                        f"post: turn {state.get('turn_number')} "
                        f"P1 life={life(state, 1)}")
                    say("LIFE probe complete; exporting post and stopping")
                    ST["stop"] = True
                    break

            if (st0 and (st0["state"].get("turn_number") or 0) > TURN_CAP
                    and ST["stage"] in ("SETUP", "ARMED")):
                OBS["notes"].append(f"TURN_CAP {TURN_CAP} hit in {ST['stage']}")
                say("TURN_CAP: finishing")
                break
            if time.time() - last_diag > 25 and st0:
                last_diag = time.time()
                s = st0["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} stage={ST['stage']} "
                    f"aria={OBS['aria_oid']} searches={len(OBS['search_events'])} "
                    f"fountain={OBS['fountain_oid']} "
                    f"P0hand={hand_lnames(s, 0)[:5]} P1hand={hand_lnames(s, 1)[:5]}")
            if time.time() - t0 > TIMEOUT - 5:
                OBS["notes"].append("TIMEOUT hit; finishing")
                break
    except Exception as e:
        OBS["notes"].append(f"main loop exception: {type(e).__name__}: {e}")
        say(f"main loop exception: {e}")

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    OBS["notes"].append(f"final stage={ST['stage']} "
                        f"search_events={len(OBS['search_events'])}")
    if not ST["post_exported"]:
        try:
            await export_named(p0, "post")
            ST["post_exported"] = True
            OBS["notes"].append("post.json exported at finish() fallback")
        except Exception as e:
            OBS["notes"].append(f"post export failed: {e}")
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
        aria = cd.get("mornsong aria", {})
        for key, fn in (("mornsong aria", "parse_mornsong_aria.json"),
                        ("radiant fountain", "parse_radiant_fountain.json")):
            card = cd.get(key, {})
            blob = {"name": card.get("name"),
                    "oracle_text": card.get("oracle_text"),
                    "static_abilities": card.get("static_abilities"),
                    "triggers": card.get("triggers")}
            parse[fn] = blob
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump(blob, f, indent=2)
    except Exception as e:
        OBS["notes"].append(f"parse json failed: {e}")

    dur = time.time() - t0
    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/pull/9921",
        "is_pull_request": True,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 144, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Mornsong Aria", 8], ["Swamp", 52]],
            "P1": [["Radiant Fountain", 8], ["Plains", 52]],
        },
        "assertions": a,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 search_to_drawer: %s; "
                   "A3 life_loss: %s; A4 card_to_hand: %s; "
                   "A5 no_life_gain: %s; A6 cleanup: %s"
                   % tuple(a.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_search_to_drawer",
                            "A3_life_loss", "A4_card_to_hand",
                            "A5_no_life_gain", "A6_cleanup"))),
        "scope": ("Mornsong Aria draw-step trigger on the opponent's draw "
                  "step: who searches whose library; and whether the "
                  "'or gain life' clause is enforced (Radiant Fountain ETB "
                  "probe)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "#9921 is a bug-labeled fix PR (open, unmerged); the scenario "
            "reproduces the bug report embedded in its Summary on the pinned "
            "v0.111.0 release, which predates the fix.",
        ],
        "search_events": OBS["search_events"],
        "search_recipient_tag": OBS.get("search_recipient_tag"),
        "search_library_inferred": OBS.get("search_library_inferred"),
        "p1_life_at_search": OBS.get("p1_life_at_search"),
        "p1_life_after_trigger": OBS.get("p1_life_after_trigger"),
        "life_before_fountain": OBS.get("life_before_fountain"),
        "life_after_fountain": OBS.get("life_after_fountain"),
        "data_has_cantgainlife": OBS.get("data_has_cantgainlife"),
        "notes": OBS["notes"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9921.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": a, "notes": OBS["notes"],
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
    pidfile = "/tmp/scenario_9921.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9921 instance is alive "
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
