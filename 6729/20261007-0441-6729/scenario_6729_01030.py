#!/usr/bin/env python3
"""Issue #6729: Green Sun's Zenith does not trigger ETB (Arboreal Grazer).

V0.103.0 RE-VALIDATION (protocol 106) of the 2026-10-05 v0.102.0 run
(run 20261005-6729; verdict reproduced with nuance: the headline ETB claim
did NOT reproduce, the "shuffled repeatedly" secondary observation DID --
3 LibraryShuffle commands vs 2 Oracle instructions).

Protocol-106 port of driver/scenario_6729_01020.py. Conventions (AGENTS.md):
  - 01030 port notes (2026-10-07): v0.103.0/ec27a8d; DeclareAttackers
    no-resubmit guard carried over (never re-submit once declared this
    turn; post-declaration priority round still advertises the action);
    render reuses the version-agnostic render_summary_6729_01020.py.
  - ClientHello protocol 106 (server enforces exact match); verified by this
    run's own raw handshake before driving.
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - MulliganDecision answered via legacy Action; bottoming via vi
    schema/select with waitingForKind 'mulligan'.
  - real_decision_pending() excludes tapLandForMana / untapLandForMana /
    castSpell / activateAbility / passPriority / candidate / mana /
    mulliganDecision menus (a schema rtype or decideOptionalEffect is
    always a real decision).
  - Land plays advertised as vi exactChoices carrying a playLand action
    code are answered explicitly BEFORE the decision gate (is_land() helper
    covers every land the seats can hold).
  - Mana payment via vi tapLandForMana with a needs dict.
  - surf_codes() None-filtered.

BEHAVIORAL CONTRACT (unchanged from the v0.85.0 run)
----------------------------------------------------
Oracle text (pinned v0.103.0 card-data.json; verified by A0 below):
  Green Sun's Zenith: "Search your library for a green creature card with mana
    value X or less, put it onto the battlefield, then shuffle. Shuffle Green
    Sun's Zenith into its owner's library."
  Arboreal Grazer: "Reach. When this creature enters, you may put a land card
    from your hand onto the battlefield tapped."

Assertions (on completed outcomes, not prompts):
  A0 data_level      Pinned card data carries the Oracle text above.
  A1 setup_ok        Zenith cast with X=1 (Grazer MV 1); search prompt offered
                     and the Grazer choice submitted.
  A2 grazer_bf       The searched Grazer moved onto P0's battlefield.
  A3 etb_triggered   Grazer's enters trigger fired (observed on the stack);
                     driver accepted the optional "you may" and the chosen
                     land from hand entered the battlefield tapped.
  A4 zenith_library  The Zenith moved from the stack into P0's library (not
                     the graveyard).
  A5 shuffle_exactly2 Exactly two LibraryShuffle commands during the Zenith
                     resolution (the two Oracle instructions).
  A6 cleanup         Stack empty, game proceeds.

Verdict rule: reproduced iff A2 passes and A3 fails with the Grazer reaching
the BF without its enters trigger (the reported bug), or A4/A5 fail with the
wrong Zenith destination / extra shuffles (the secondary observations).

Scenario (native engine, two human-client seats):
  P0: 12x Green Sun's Zenith + 12x Arboreal Grazer + 36x Forest. Mulligan
      hunt: keeps a hand with a Zenith and >=2 Forests (max 4 mulls), plays
      a Forest per turn, casts the Zenith with X=1 once 2 Forests are
      untapped, searches the Grazer, answers its ETB.
  P1: 60x Island. Plays a land per turn, always passes priority, never
      attacks.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
from client import PhaseClient, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261007-0441-6729"
ISSUE = 6729
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

PIN = "v0.103.0"
SERVER_IDENTITY = {
    "server_version": PIN,
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.103.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) at pin time 2026-10-06; "
                       "binary digest matches GitHub's asset digest; data "
                       "digests match the signed manifest; digests recomputed "
                       "against on-disk files by this run"),
    "server_run_id": ("backfill-owned isolated v0.103.0 server on "
                      "127.0.0.1:9374 (runs/cron-20261007-0441/games.db; "
                      "started by this run; ServerHello verified by this "
                      "run's own raw handshake)"),
    "mode": "Full",
    "source": ("2026-10-07: latest stable release v0.103.0 (published "
               "2026-10-06T16:45:47Z) == pinned release dir; ServerHello "
               "0.103.0/ec27a8d/protocol 106 verified by this run"),
}
for _f, _k in (("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.103.0/data/draft-pools.json", "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = hashlib.sha256(
        open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(
    open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

ZENITH = "green sun's zenith"
GRAZER = "arboreal grazer"
FOREST = "forest"
ISLAND = "island"

P0_DECK = deck((ZENITH, 12), (GRAZER, 12), (FOREST, 36))
P1_DECK = deck((ISLAND, 60))

SUBMITTED_OPPS = set()
MULLS = set()
PASSED_REV = {}
LAND_PLAYED_TURN = {}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def gy_names(state, pid):
    return [obj_lname(state, o)
            for o in player_of(state, pid).get("graveyard", [])]


def lib_names(state, pid):
    return [obj_lname(state, o)
            for o in player_of(state, pid).get("library", [])]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_land_oids(state, pid, lname):
    return [o for o in bf_by_name(state, pid, lname)
            if not get_obj(state, o).get("tapped")]


def zenith_on_stack(state):
    for e in state.get("stack", []) or []:
        if "zenith" in json.dumps(e, default=str).lower():
            return e
    return None


def grazer_trigger_on_stack(state, grazer_oid=None):
    for e in state.get("stack", []) or []:
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else str(kind)
        if ktype == "TriggeredAbility":
            if grazer_oid is not None and e.get("source_id") is not None:
                try:
                    if int(e["source_id"]) == int(grazer_oid):
                        return e
                except (TypeError, ValueError):
                    pass
            else:
                blob = json.dumps(e, default=str).lower()
                if "arboreal grazer" in blob or "grazer" in blob:
                    return e
            continue
        blob = json.dumps(e, default=str).lower()
        if ("triggered" in blob and "arboreal grazer" in blob):
            return e
    return None


def shuffle_commands(state):
    """resolved_rules_journal entries whose command mentions a shuffle."""
    out = []
    j = state.get("resolved_rules_journal", {}) or {}
    for e in j.get("entries", []) or []:
        cmd = e.get("command", {}) or {}
        keys = [k for k in cmd.keys()
                if "shuffle" in str(k).lower()]
        if keys:
            out.append((keys[0], e))
    return out

# ------------------------------------------------------------- vi machinery
def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = dict(p)
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
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
            d = s.get("data") or {}
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    """None-filtered (106 surfaces sometimes carry data:{} with no code)."""
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data")
        if isinstance(d, dict):
            c = d.get("code")
            if c:
                out.append(c)
    return out


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference"), (d.get("zone") or "")
    return None, ""


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True iff the viewing seat has a real decision. The 106 engine offers
    tapLandForMana / castSpell / activateAbility / playLand choice menus at
    ordinary priority windows; those must not block priority passes -- but
    playLand is answered explicitly by the driver before this gate."""
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


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "passPriority" in surf_codes(ch) and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


# ------------------------------------------------------------- mulligan / bottom / discard
def do_mulligan_hunt(c, acts, st, pid, tag, want, max_mulls):
    """Protocol 106: MulliganDecision arrives as a legacy legal action.
    Answer Mulligan while the hand misses `want` (up to max_mulls), else Keep.
    At most one answer per (tag, interaction-or-revision)."""
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
    state = st["state"]
    hn = hand_names(state, pid)
    n_mulls = sum(1 for k in MULLS if k[0] == tag and k[1] == "mull") - 1
    if want(hn) or n_mulls >= max_mulls:
        say(f"[{tag}] keep {len(hn)} (mulls={n_mulls}): {hn[:8]}")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
        decision = "Keep"
    else:
        say(f"[{tag}] mulligan #{n_mulls + 1} (hand={hn[:8]})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        decision = "Mulligan"
    return ("MulliganDecision", {"choice": {"type": decision}})


async def do_bottom(c, acts, st, pid, tag, bkey):
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code 'mulligan')."""
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
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    ranked = sorted(cands, key=lambda ch: bkey(state, ch))
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, cand_reference(x)[0]) for x in picks]} via vi")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}},
                      tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type") or "select"
        cands = rdata.get("candidates") or rdata.get("choices") or []
        handset = set(hand)
        if not any(cand_reference(ch)[0] in handset or
                   str(cand_reference(ch)[0]) in {str(h) for h in handset}
                   for ch in cands):
            continue

        def rank(ch):
            oid = cand_reference(ch)[0]
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm == GRAZER:
                return (0, nm)
            if nm == ZENITH:
                return (3, nm)
            if oid is not None and is_land(get_obj(state, oid)):
                return (2, nm)
            return (1, nm)

        picks = [ch["id"] for ch in sorted(cands, key=rank)[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


# ------------------------------------------------------------- mana payment
async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            if "tapLandForMana" in surf_codes(ch):
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
            f"(needs now {needs})")
        await answer_vi(c, opp, pick, tag)
        return True
    return False


# ------------------------------------------------------------- cast / land
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


def vi_cast_choice(st, state, pid, key):
    """A vi exactChoices/choose opportunity whose castSpell choice references
    a `key`-named card in pid's hand."""
    handset = {str(o) for o in hand_ids(state, pid)}
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            ref, _ = cand_reference(ch)
            if ref is not None and str(ref) in handset \
                    and obj_lname(state, ref) == key:
                return opp, ch
    return None, None


async def play_land_vi(c, st, state, pid, tag):
    """Answer a vi playLand opportunity with a land from pid's hand (one
    land per turn)."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    handset = {str(o) for o in hand_ids(state, pid)}
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref, _ = cand_reference(ch)
            if ref is not None and str(ref) in handset \
                    and is_land(get_obj(state, ref)):
                iid = opp.get("interactionId")
                key = (tag, "playland", iid, str(ref))
                if key in SUBMITTED_OPPS:
                    return False
                SUBMITTED_OPPS.add(key)
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, ref)} via vi")
                await answer_vi(c, opp, ch, tag)
                return True
    return False


async def play_land_legacy(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)} via legacy")
                    await submit_as_is(c, a)
                    return True
    return False

# ------------------------------------------------------------- scenario state
OBS = {
    "zenith_cast": False,
    "x_chosen": None,
    "search_submitted": False,
    "grazer_oid": None,
    "trigger_seen": False,
    "trigger_entry": None,
    "optional_accepted": False,
    "land_put_oid": None,
    "mid_exported": False,
    "pre_exported": False,
    "post_exported": False,
    "pre_bf_forest": None,
    "pre_hand_forest": None,
    "done": False,
    "rejections": [],
    "mana_needs": {},
}
SHAPES_LOGGED = set()
NOTES = []
T0 = time.time()

c0 = None
c1 = None


def want_p0(hn):
    return ZENITH in hn and sum(1 for n in hn if n == FOREST) >= 2


def bkey_p0(state, ch):
    oid = cand_reference(ch)[0]
    nm = obj_lname(state, oid) if oid is not None else "?"
    if nm == GRAZER:
        return (0, str(oid))
    if nm == ZENITH:
        return (3, str(oid))
    if oid is not None and is_land(get_obj(state, oid)):
        return (2, str(oid))
    return (1, str(oid))


async def export_state(tag):
    s = await c0.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json ({len(s)} bytes)")
    wire("export", {"tag": tag, "bytes": len(s)})


def record_pre_counts():
    try:
        pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
        OBS["pre_bf_forest"] = len(bf_by_name(pre_st, 0, FOREST))
        OBS["pre_hand_forest"] = hand_names(pre_st, 0).count(FOREST)
        say(f"pre: bf_forest={OBS['pre_bf_forest']} "
            f"hand_forest={OBS['pre_hand_forest']}")
    except Exception as e:
        NOTES.append(f"pre count recording failed: {e}")


async def scan_scenario(c, st, state, pid, tag):
    """Zenith X=1, the Grazer 'you may', the library search (pre-exported
    first), and the ETB land-from-hand target. Mana/priority menus are
    never touched here."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        spec = data.get("spec", {}) or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        items = data.get("candidates") or data.get("choices") or []
        if pid == 0:
            texts = [choice_text(ch) for ch in items]
            blob = " // ".join(texts)
            codes = set()
            for ch in items:
                codes.update(surf_codes(ch))
            key = (tag, rtype, spec_type, blob[:80], tuple(sorted(codes))[:4])
            if key not in SHAPES_LOGGED:
                SHAPES_LOGGED.add(key)
                say(f"[{tag}] interaction rtype={rtype} spec={spec_type} "
                    f"n={len(items)} codes={sorted(codes)[:5]} "
                    f"choices=[{blob[:200]}]")
                wire("interaction_shape",
                     {"who": tag, "rtype": rtype, "spec_type": spec_type,
                      "codes": sorted(codes)[:8], "n": len(items)})
        if pid != 0:
            continue
        # --- Zenith X-choice (schema number) -> X=1 ---
        if rtype == "schema" and spec_type == "number" \
                and OBS["x_chosen"] is None:
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": 1}}}
            say("[P0] Zenith X-choice -> X=1")
            await interact_as(c, sub, tag)
            SUBMITTED_OPPS.add(iid)
            OBS["x_chosen"] = 1
            return True
        # codes for this opportunity (all seats); the priority/mana skip
        # lives at the end of this per-opp chain so real schema selects
        # (Zenith search, ETB land target) are answered first.
        codes = set()
        for ch in items:
            codes.update(surf_codes(ch))
        # --- optional "you may" (Grazer ETB) -> accept ---
        if "decideOptionalEffect" in codes:
            pick = None
            for ch in items:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if s.get("type") == "value" \
                            and d.get("role") == "accept" \
                            and str(d.get("value")).lower() == "true":
                        pick = ch
                        break
                if pick is not None:
                    break
            if pick is None:
                NOTES.append(f"optional prompt without accept choice "
                             f"(iid={iid}); skipping")
                continue
            await answer_vi(c, opp, pick, tag)
            SUBMITTED_OPPS.add(iid)
            OBS["optional_accepted"] = True
            say("[P0] Grazer ETB optional 'you may' -> ACCEPT")
            return True
        # --- library search -> pick the Grazer (export pre FIRST).
        # Gated on the Zenith actually being cast with X chosen: the
        # castSpell priority menu also lists hand cards (including Grazers)
        # and must never be mistaken for the search prompt. ---
        grazer_ch = None
        if (OBS["zenith_cast"] and OBS["x_chosen"] == 1
                and not OBS["search_submitted"]
                and "castSpell" not in codes and "playLand" not in codes
                and "tapLandForMana" not in codes):
            for ch in items:
                ref, _ = cand_reference(ch)
                nm = obj_lname(state, ref) if ref is not None else ""
                if "arboreal grazer" in choice_text(ch).lower() \
                        or nm == GRAZER:
                    grazer_ch = ch
                    break
        if grazer_ch is not None:
            if not OBS["pre_exported"]:
                await export_state("pre")
                OBS["pre_exported"] = True
                record_pre_counts()
            if rtype == "exactChoices":
                sub = {"interactionId": iid, "response":
                       {"type": "choose",
                        "data": {"choiceId": grazer_ch["id"]}}}
            else:
                stype = spec_type or "sequence"
                sub = {"interactionId": iid, "response":
                       {"type": stype,
                        "data": {"choiceIds": [grazer_ch["id"]]}}}
            say("[P0] Zenith search -> Arboreal Grazer")
            await interact_as(c, sub, tag)
            SUBMITTED_OPPS.add(iid)
            OBS["search_submitted"] = True
            return True
        # --- ETB land-from-hand target (schema, hand-land candidates) ---
        # Guarded: only the trigger's own resolution target (optional was
        # accepted, game not finished), so a mulligan-bottom or other
        # schema/select elsewhere can never be mistaken for it.
        if rtype == "schema" and spec_type in ("select", "sequence") \
                and OBS["optional_accepted"] and not OBS["post_exported"]:
            handset = {str(o) for o in hand_ids(state, pid)}
            forest_ch = None
            for ch in items:
                ref, zone = cand_reference(ch)
                if ref is not None and str(ref) in handset \
                        and str(zone).lower() == "hand" \
                        and obj_lname(state, ref) == FOREST:
                    forest_ch = ch
                    break
            if forest_ch is not None:
                sub = {"interactionId": iid, "response":
                       {"type": spec_type,
                        "data": {"choiceIds": [forest_ch["id"]]}}}
                say("[P0] Grazer ETB land target -> Forest "
                    f"(ref={cand_reference(forest_ch)[0]})")
                await interact_as(c, sub, tag)
                SUBMITTED_OPPS.add(iid)
                OBS["land_put_oid"] = str(cand_reference(forest_ch)[0])
                return True
        # --- priority/mana menus: never touch ---
        if codes and codes <= NON_DECISION_CODES:
            continue
    return False


async def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            return
        if t in ("Error", "ActionRejected"):
            rec = {"who": c.name, "type": t,
                   "data": json.dumps(data, default=str)[:500]}
            OBS["rejections"].append(rec)
            say(f"[{c.name}] {t}: {rec['data'][:300]}")
            wire("rejection", rec)


async def p0_tick(st):
    state = st["state"]
    acts = merged_actions(st)
    m = do_mulligan_hunt(c0, acts, st, 0, "P0", want_p0, 4)
    if m:
        await submit_as_is(c0, {"type": m[0], "data": m[1]})
        return
    if await do_bottom(c0, acts, st, 0, "P0", bkey_p0):
        return
    if await do_discard_to_handsize(c0, acts, st, 0, "P0"):
        return
    atypes = {a.get("type") for a in acts}
    if "DeclareAttackers" in atypes:
        # 2026-10-07 guard: after a declaration the game runs a
        # post-declaration priority round with DeclareAttackers still
        # advertised; never re-submit in the same turn -- fall through
        # to priority handling instead.
        turn = state.get("turn_number")
        dkey = ("declared_attackers", "P0", turn)
        if dkey not in SUBMITTED_OPPS:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                SUBMITTED_OPPS.add(dkey)
                await submit_as_is(c0, d)
                return
        # already declared this turn: fall through to priority below
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c0, acts):
        return
    needs = OBS["mana_needs"]
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c0, st, "P0", needs):
            return
    await asyncio.sleep(0)  # yield to the pump before leg evaluation
    st = c0.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if zenith_on_stack(state) and OBS["mana_needs"]:
        OBS["mana_needs"] = {}  # payment completed with the cast on the stack
        say("[P0] mana needs cleared (Zenith on stack)")
    if await scan_scenario(c0, st, state, 0, "P0"):
        return
    grazers = bf_by_name(state, 0, GRAZER)
    if grazers and OBS["grazer_oid"] is None:
        OBS["grazer_oid"] = grazers[0]
        say(f"Grazer on battlefield: oid {grazers[0]}")
        wire("grazer_on_bf", {"oid": grazers[0]})
    hit = grazer_trigger_on_stack(state, OBS["grazer_oid"])
    if hit and not OBS["trigger_seen"]:
        OBS["trigger_seen"] = True
        OBS["trigger_entry"] = json.dumps(hit, default=str)[:1500]
        say("GRAZER ETB TRIGGER observed on stack")
        wire("grazer_trigger_on_stack", hit)
        await export_state("mid_trigger")
        OBS["mid_exported"] = True
    # pre-export fallback (Zenith on the stack, Grazer still out)
    if zenith_on_stack(state) and OBS["x_chosen"] is not None \
            and not OBS["pre_exported"] and not grazers:
        await export_state("pre")
        OBS["pre_exported"] = True
        record_pre_counts()
    # post condition: Zenith resolved, Grazer on BF, stack empty
    if OBS["zenith_cast"] and grazers and not zenith_on_stack(state) \
            and len(state.get("stack", []) or []) == 0 \
            and not OBS["post_exported"]:
        await export_state("post")
        OBS["post_exported"] = True
        OBS["done"] = True
        say(f"post: turn={state.get('turn_number')} "
            f"phase={state.get('phase')}; resolution complete")
        return
    # main-phase: land, then the Zenith
    if my_main(state, 0):
        if await play_land_vi(c0, st, state, 0, "P0"):
            return
        if await play_land_legacy(c0, state, 0, acts, "P0"):
            return
        if not OBS["zenith_cast"] \
                and len(untapped_land_oids(state, 0, FOREST)) >= 2:
            for oid in hand_ids(state, 0):
                if obj_lname(state, oid) == ZENITH:
                    a, _ = cast_action_for(acts, state, ZENITH)
                    if a is not None:
                        OBS["mana_needs"] = {"generic": 1, "G": 1}
                        await submit_as_is(c0, a)
                        OBS["zenith_cast"] = True
                        say("P0 casts Green Sun's Zenith (legacy action)")
                        wire("cast", {"how": "legacy", "needs": {"generic": 1, "G": 1}})
                        return
                    opp, ch = vi_cast_choice(st, state, 0, ZENITH)
                    if opp is not None:
                        OBS["mana_needs"] = {"generic": 1, "G": 1}
                        await answer_vi(c0, opp, ch, "P0")
                        OBS["zenith_cast"] = True
                        say("P0 casts Green Sun's Zenith (vi castSpell)")
                        wire("cast", {"how": "vi", "needs": {"generic": 1, "G": 1}})
                        return
                    break
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get("P0", -1) < c0.revision:
            if await pass_priority(c0, st, acts):
                PASSED_REV["P0"] = c0.revision


async def p1_tick(st):
    state = st["state"]
    acts = merged_actions(st)
    m = do_mulligan_hunt(c1, acts, st, 1, "P1", lambda hn: True, 0)
    if m:
        await submit_as_is(c1, {"type": m[0], "data": m[1]})
        return
    if await do_discard_to_handsize(c1, acts, st, 1, "P1"):
        return
    atypes = {a.get("type") for a in acts}
    if "DeclareAttackers" in atypes:
        # 2026-10-07 guard: after a declaration the game runs a
        # post-declaration priority round with DeclareAttackers still
        # advertised; never re-submit in the same turn -- fall through
        # to priority handling instead.
        turn = state.get("turn_number")
        dkey = ("declared_attackers", "P1", turn)
        if dkey not in SUBMITTED_OPPS:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                SUBMITTED_OPPS.add(dkey)
                await submit_as_is(c1, d)
                return
        # already declared this turn: fall through to priority below
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c1, acts):
        return
    if my_main(state, 1):
        if await play_land_vi(c1, st, state, 1, "P1"):
            return
        if await play_land_legacy(c1, state, 1, acts, "P1"):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get("P1", -1) < c1.revision:
            if await pass_priority(c1, st, acts):
                PASSED_REV["P1"] = c1.revision

# ------------------------------------------------------------- main
async def verify_server_hello():
    url = os.environ["PHASE_WS_URL"]
    async with websockets.connect(url, max_size=10_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 10)
    hello = json.loads(raw)
    assert hello.get("type") == "ServerHello", \
        f"expected ServerHello, got {hello.get('type')}"
    d = hello.get("data", {}) or {}
    obs = {"server_version": d.get("server_version"),
           "build_commit": d.get("build_commit"),
           "protocol_version": d.get("protocol_version"),
           "mode": d.get("mode")}
    want = {"server_version": "0.103.0", "build_commit": "ec27a8d",
            "protocol_version": 106, "mode": "Full"}
    for k, w in want.items():
        assert str(obs[k]) == str(w), \
            f"ServerHello {k}={obs[k]!r} != pinned {w!r}; refusing to run"
    say(f"ServerHello OK: {obs}")
    wire("server_hello", obs)


def check_data_level():
    z = CARD_DATA.get("green sun's zenith", {})
    g = CARD_DATA.get("arboreal grazer", {})
    zo = str(z.get("oracle_text", ""))
    go = str(g.get("oracle_text", ""))
    ok = ("Search your library" in zo
          and "Shuffle Green Sun's Zenith into its owner's library" in zo
          and "When" in go and "enters" in go
          and "land card from your hand onto the battlefield tapped" in go)
    ev = {"zenith_oracle": zo, "grazer_oracle": go,
          "zenith_abilities": z.get("abilities"),
          "grazer_triggers": g.get("triggers"), "ok": ok}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    say(f"data-level check: ok={ok}")
    wire("data_level", {"ok": ok})
    return ok


async def main():
    global c0, c1
    data_ok = check_data_level()
    await verify_server_hello()
    c0 = PhaseClient("P0")
    c1 = PhaseClient("P1")
    await c0.connect()
    await c1.connect()
    say("P0 hello done; P1 hello done")
    attached = await c0.create(P0_DECK, player_count=2)
    code = c0.game_code
    say(f"game created: code={code}")
    wire("game_created", {"code": code, "attached": attached})
    j1 = await c1.join(code, P1_DECK)
    say(f"P1 joined: {json.dumps(j1, default=str)[:160]}")
    wire("game_joined", {})

    last_sig = None
    stall_watch = time.time()
    try:
        while time.time() - T0 < 900 and not OBS["done"]:
            await asyncio.sleep(0.2)
            await drain(c0)
            await drain(c1)
            try:
                if c0.latest:
                    await p0_tick(c0.latest)
            except Exception as e:
                say(f"[P0] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": "P0",
                                    "err": f"{type(e).__name__}: {e}"})
            try:
                if c1.latest:
                    await p1_tick(c1.latest)
            except Exception as e:
                say(f"[P1] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": "P1",
                                    "err": f"{type(e).__name__}: {e}"})
            st = c0.latest or {}
            state = st.get("state") or {}
            sig = (state.get("turn_number"), state.get("phase"),
                   state.get("active_player"),
                   len(state.get("stack", []) or []))
            if sig != last_sig:
                last_sig = sig
                stall_watch = time.time()
                say(f"[tick] turn={sig[0]} phase={sig[1]} active={sig[2]} "
                    f"stack={sig[3]} rev={c0.revision}")
            if OBS["done"]:
                break
            if time.time() - stall_watch > 240:
                NOTES.append(f"stall watchdog: no state progress for 240s "
                             f"at {sig}")
                say("STALL WATCHDOG fired")
                break
            if (state.get("turn_number") or 0) > 15 and not OBS["zenith_cast"]:
                NOTES.append("turn cap reached without the Zenith cast")
                say("turn cap reached without cast")
                break
    finally:
        await finalize(data_ok, code)
        await c0.close()
        await c1.close()
        if not WIRE.closed:
            WIRE.close()
        if not RUNLOG.closed:
            RUNLOG.close()


def load_env(tag):
    try:
        return json.loads(open(f"{EVDIR}/{tag}.json").read())["state"]
    except Exception as e:
        NOTES.append(f"{tag}.json load failed: {e}")
        return None


async def finalize(data_ok, game_code):
    ass = {k: "not-run" for k in
           ("A0_data_level", "A1_setup_ok", "A2_grazer_bf",
            "A3_etb_triggered", "A4_zenith_library", "A5_shuffle_exactly2",
            "A6_cleanup")}
    ass["A0_data_level"] = "passed" if data_ok else "failed"
    if not data_ok:
        NOTES.append("A0 FAILED: pinned card data does not carry the "
                     "expected Oracle text")

    pre_st = load_env("pre")
    post_st = load_env("post")

    if OBS["zenith_cast"] and OBS["x_chosen"] == 1 \
            and OBS["search_submitted"]:
        ass["A1_setup_ok"] = "passed"
    else:
        ass["A1_setup_ok"] = "failed"
        NOTES.append(f"A1: cast={OBS['zenith_cast']} x={OBS['x_chosen']} "
                     f"search={OBS['search_submitted']}")

    if post_st is not None:
        grazers = bf_by_name(post_st, 0, GRAZER)
        if grazers:
            ass["A2_grazer_bf"] = "passed"
            OBS["grazer_oid"] = grazers[0]
        else:
            ass["A2_grazer_bf"] = "failed"
            NOTES.append("A2: no Arboreal Grazer on P0 battlefield in post.json")
        post_bf_forest = len(bf_by_name(post_st, 0, FOREST))
        post_hand_forest = hand_names(post_st, 0).count(FOREST)
        chosen_ok = False
        if OBS["land_put_oid"] is not None:
            co = get_obj(post_st, OBS["land_put_oid"])
            chosen_ok = (co.get("zone") == "Battlefield" and co.get("tapped"))
        if OBS["trigger_seen"] and OBS["optional_accepted"] \
                and post_bf_forest - (OBS["pre_bf_forest"] or 0) >= 1 \
                and chosen_ok \
                and post_hand_forest <= (OBS["pre_hand_forest"] or 0):
            ass["A3_etb_triggered"] = "passed"
        else:
            ass["A3_etb_triggered"] = "failed"
            NOTES.append(
                f"A3: trigger_seen={OBS['trigger_seen']} "
                f"optional_accepted={OBS['optional_accepted']} "
                f"bf_forest {OBS['pre_bf_forest']}->{post_bf_forest} "
                f"chosen land {OBS['land_put_oid']} bf+tapped={chosen_ok} "
                f"hand_forest {OBS['pre_hand_forest']}->{post_hand_forest}")
        post_lib = lib_names(post_st, 0)
        post_gy = gy_names(post_st, 0)
        zen_in_lib = post_lib.count(ZENITH)
        zen_in_gy = post_gy.count(ZENITH)
        if zen_in_lib >= 1 and zen_in_gy == 0:
            ass["A4_zenith_library"] = "passed"
        else:
            ass["A4_zenith_library"] = "failed"
            NOTES.append(f"A4: Zenith in library={zen_in_lib}, "
                         f"in graveyard={zen_in_gy}")
        if pre_st is not None:
            pre_sh = shuffle_commands(pre_st)
            post_sh = shuffle_commands(post_st)
            new_shuffles = len(post_sh) - len(pre_sh)
            OBS["shuffles_pre"] = len(pre_sh)
            OBS["shuffles_post"] = len(post_sh)
            OBS["shuffle_kinds"] = sorted({k for k, _ in post_sh})
            if new_shuffles == 2:
                ass["A5_shuffle_exactly2"] = "passed"
            else:
                ass["A5_shuffle_exactly2"] = "failed"
                NOTES.append(
                    f"A5: {new_shuffles} shuffle commands during the Zenith "
                    f"resolution (pre={len(pre_sh)}, post={len(post_sh)}, "
                    f"kinds={OBS['shuffle_kinds']}); expected 2")
        else:
            ass["A5_shuffle_exactly2"] = "not-run"
            NOTES.append("A5: not-run (pre.json missing)")
        if len(post_st.get("stack", []) or []) == 0:
            ass["A6_cleanup"] = "passed"
        else:
            ass["A6_cleanup"] = "failed"
            NOTES.append(f"A6: stack not empty in post.json "
                         f"({len(post_st.get('stack', []))} entries)")
    else:
        for k in ("A2_grazer_bf", "A3_etb_triggered", "A4_zenith_library",
                  "A6_cleanup"):
            ass[k] = "not-run"
        NOTES.append("A2/A3/A4/A6: not-run (post.json missing)")
        ass["A5_shuffle_exactly2"] = "not-run"

    if ass["A0_data_level"] == "failed" or ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
        NOTES.append("verdict=blocked: setup never completed; no trustworthy "
                     "result on the reported outcome")
    elif ass["A2_grazer_bf"] == "passed" \
            and ass["A3_etb_triggered"] == "failed":
        verdict = "reproduced"
        NOTES.append("verdict=reproduced: the searched Grazer reached the "
                     "battlefield but its enters trigger never fired/resolved")
    elif ass["A4_zenith_library"] == "failed" \
            or ass["A5_shuffle_exactly2"] == "failed":
        verdict = "reproduced"
        NOTES.append("verdict=reproduced (secondary observation from the "
                     "report): the ETB itself fired and resolved, but the "
                     "Zenith resolution deviates from the report's expected "
                     "behavior (wrong destination and/or extra shuffles)")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        NOTES.append("verdict=blocked: incomplete assertion set; no "
                     "trustworthy result")

    say("assertions: " + json.dumps(ass))
    say("verdict: " + verdict)
    for n_ in NOTES:
        say("note: " + n_)

    duration_s = round(time.time() - T0, 1)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "server": SERVER_IDENTITY,
        "decks": {"P0": [(ZENITH, 12), (GRAZER, 12), (FOREST, 36)],
                  "P1": [(ISLAND, 60)]},
        "games": [{
            "tag": "A",
            "desc": "Green Sun's Zenith X=1 tutors Arboreal Grazer; the Grazer "
                    "ETB ('you may put a land card from your hand onto the "
                    "battlefield tapped') is accepted and a Forest is put "
                    "tapped; Zenith destination and shuffle counts asserted",
            "assertions": ass,
            "observations": {
                "zenith_cast": OBS["zenith_cast"],
                "x_chosen": OBS["x_chosen"],
                "search_submitted": OBS["search_submitted"],
                "grazer_oid": OBS["grazer_oid"],
                "trigger_seen": OBS["trigger_seen"],
                "trigger_entry": OBS["trigger_entry"],
                "optional_accepted": OBS["optional_accepted"],
                "land_put_oid": OBS["land_put_oid"],
                "pre_bf_forest": OBS["pre_bf_forest"],
                "pre_hand_forest": OBS["pre_hand_forest"],
                "shuffles_pre": OBS.get("shuffles_pre"),
                "shuffles_post": OBS.get("shuffles_post"),
                "shuffle_kinds": OBS.get("shuffle_kinds"),
                "rejections": OBS["rejections"],
            },
            "notes": NOTES,
        }],
        "verdict": verdict,
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
        "duration_s": duration_s,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0)),
        "driver_notes": [
            "Protocol-106 port of scenario_6729_085.py (verified 2026-09-17, "
            "run 20260917-6729): ClientHello protocol 106 exact match; "
            "CreateGameWithSettings + JoinGameWithPassword + start_when_full; "
            "MulliganDecision via legacy Action with a keep-hunt (Zenith + "
            ">=2 Forests, max 4 mulls); bottoming via vi schema/select with "
            "waitingForKind 'mulligan'.",
            "real_decision_pending excludes tapLandForMana/untapLandForMana/"
            "castSpell/activateAbility/passPriority/candidate/mana/"
            "mulliganDecision menus; vi playLand opportunities answered "
            "explicitly before the decision gate (is_land helper); mana "
            "payment via vi tapLandForMana with needs={generic:1,G:1}; "
            "surf_codes None-filtered.",
            "pre.json is exported immediately before the search-choice "
            "submission, so the resolved_rules_journal diff (pre->post) "
            "captures exactly the Zenith resolution's shuffle commands.",
            "2026-10-05 fix (run 20261005-6729): on protocol 106 the "
            "Zenith search and the ETB land target arrive as schema "
            "selects whose only surface code is 'candidate', which the "
            "NON_DECISION_CODES skip swallowed before the search/ETB "
            "branches could answer them (stall at the search select; "
            "the earlier 0941 run died at the land select the same way). "
            "The skip now runs AFTER the optional/search/ETB-land "
            "branches, and the ETB-land branch is additionally guarded "
            "by optional_accepted and not post_exported.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # assertions.json (flat, for the evidence index)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(ass, f, indent=1)

    # server excerpts for this game
    try:
        slog = f"{BACKFILL}/runs/{RUN_ID}/server.log"
        excerpts = []
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    if game_code in line:
                        excerpts.append(line.rstrip())
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines for {game_code}")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    # scenario + render sources for provenance
    import shutil
    shutil.copy(__file__, f"{EVDIR}/scenario_6729_01030.py")
    rs = f"{BACKFILL}/driver/render_summary_6729_01020.py"
    if os.path.exists(rs):
        shutil.copy(rs, f"{EVDIR}/render_summary_6729_01020.py")

    # manifest (covers every file except itself)
    names = sorted(n for n in os.listdir(EVDIR) if n != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for n in names:
            h = hashlib.sha256(open(f"{EVDIR}/{n}", "rb").read()).hexdigest()
            mf.write(f"{h}  {n}\n")
    say(f"manifest.sha256 written ({len(names)} files)")
    print(f"VERDICT:{verdict}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
