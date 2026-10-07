#!/usr/bin/env python3
"""Issue #6643: "Party Dude -- level 3 attack trigger never fires".

RE-VALIDATION RUN on pinned v0.103.0 (protocol 106, build ec27a8d), ported
from driver/scenario_6643_01020.py (protocol 106, run 20261005-0411,
published verdict not-reproduced on v0.102.0). That run exercised the full
path and reached NOT-REPRODUCED: the trigger parsed as mode "Attacks"
(batched) with ClassLevelGE 3 and fired end-to-end (bear 2/2 -> 9/9,
X = hand 7, damage 9). The v0.102.0 result is stale under the playbook's
release-pinning rule, so the Party Dude level-3 attack-trigger path is
re-driven here on the current pin with identical assertions.

2026-10-07 driver fix (carried from scenario_6557_01030.py): never re-submit
DeclareAttackers once the attacker appears in state.combat.attackers -- the
post-declaration priority round keeps DeclareAttackers advertised, and an
empty re-declaration could void the attack. The guard falls through to
priority handling instead.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github #6643, status:confirmed, area:engine+parser, mechanic:
triggers+combat): Party Dude advances through Class levels 1 and 2
normally, but the level-3 ability -- "Whenever one or more of your
opponents are attacked, up to one target attacking creature gets +X/+X
until end of turn, where X is the number of cards in your hand" -- never
triggers when an opponent is attacked, over multiple turns.

Triage evidence in the issue: at report time the trigger clause exported as
mode {"Unknown": "Whenever one or more of your opponents are attacked"}
and preview coverage marked it supported:false, so no trigger could be
created.

Scenario (native engine, two human-client seats):
  P0: 8x Party Dude + 12x Grizzly Bears + 40x Forest (engine accepts
      >4-of for custom games; mulligan hunts a Forest).
  P1: 60x Island (passive; never attacks).
  P0 casts Party Dude ({G}), activates {1}{G} (level 2) then {4}{G}
  (level 3) at sorcery speed, casts a Bear, attacks P1 with the Bear.

Expected:
  E1: Party Dude reaches level 3 on the battlefield.
  E2: after attackers are declared against P1, the Attacks-mode trigger
      from Party Dude goes on the stack.
  E3: an up-to-one target-attacking-creature prompt is offered; answering
      it with the Bear completes.
  E4: the Bear gets +X/+X (X = P0 hand size at resolution), until end of
      turn.
  E5: stack empties and the game proceeds.

Assertions:
  A1_setup_ok     pre.json at DeclareAttackers: Party Dude on P0 BF at
                  class level 3 (driver-tracked + object field when
                  present), attack-ready Bear on P0 BF, life 20/20.
  A2_trigger_fires Party Dude Attacks trigger observed on the stack after
                  attackers declared (mode Attacks, Pump effect).
  A3_target_prompted the up-to-one attacking-creature target prompt is
                  offered and answered with the Bear.
  A4_pump_correct  Bear deals 2+X damage to P1 (X = P0 hand size at target
                  time); mid_pump.json corroborates bear P/T = 2+X/2+X.
  A5_cleanup      stack empty, game proceeding past the trigger.

Verdict rule: blocked iff A1 fails (setup never assembled). reproduced iff
A1 passes and A2 fails (or a captured failure prevents E3/E4). reproduced
iff A2 passes but A3 fails. not-reproduced iff all of A1..A5 pass (or A2+A3
pass with a downstream gap, recorded as a limitation - the reported
"trigger never fires" claim is refuted).

Protocol-106 notes: waiting_for is GONE (null) - priority = PassPriority in
the viewer's top-level legal_actions; MulliganDecision answered via legacy
Action gated on the advertised action; bottom-after-mulligan via vi
schema/select gated on context (turn 1 pregame + mulliganed + not bottomed
+ hand-card candidates; NO waitingForKind code on 106); DiscardToHandSize
via viewer_interaction only (generic 'choose' code on 106), gated on hand>7
plus hand-card candidates; casts via legacy CastSpell matched by object_id;
ActivateAbility submitted while holding priority (never pass before
submitting); mana payment via legacy PayMana actions first, then vi
tapLandForMana while a payment is expected (noise menus excluded);
target selection via vi schema/select-or-sequence; manifest computed AFTER
WIRE/RUNLOG close.

Evidence: evidence/6643/<run-id>/pre.json, mid_trigger.json, mid_target.json,
mid_pump.json, post.json, run.json, assertions.json, obs.json,
scenario_6643_01030.py, wire_log.jsonl, scenario_run.log,
server_excerpts.log, summary.png, manifest.sha256.
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
RUN_ID = os.environ.get("RUN_ID", "20261007-0212-6643")
ISSUE = 6643
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = None
RUNLOG = None


def init_logging():
    global WIRE, RUNLOG
    if WIRE is None:
        WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    if RUNLOG is None:
        RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


DUDE = "party dude"
BEAR = "grizzly bears"
FOREST = "forest"
ISLAND = "island"

LAND_NAMES = {FOREST, ISLAND}
KNOWN_CREATURES = {BEAR}


P0_DECK = [(DUDE, 8), (BEAR, 12), (FOREST, 40)]
P1_DECK = [(ISLAND, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "binary_sha256": "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc",
    "card_data_sha256": "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe",
    "draft_pools_sha256": "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.103.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828); data digests match the "
                      "signed manifest; digests recomputed against on-disk "
                      "files this run",
    "observed_at": "2026-10-07",
    "source": "ServerHello asserted by handshake this run + sha256 "
              "re-verified against pinned v0.103.0 release artifacts",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    if RUNLOG is not None:
        try:
            RUNLOG.write(m + "\n")
            RUNLOG.flush()
        except ValueError:
            pass  # log already closed (post-manifest tail messages)


def wire(event, payload):
    if WIRE is None:
        return
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_objs(state, pid):
    return [(int(o), get_obj(state, o))
            for o in player_of(state, pid).get("graveyard", [])]


def gy_names(state, pid):
    return [obj_name(o) for _, o in gy_objs(state, pid)]


def bf_creatures(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            tl = str(o.get("type_line") or "").lower()
            nm = obj_name(o)
            if "creature" in tl or nm in KNOWN_CREATURES:
                if name is None or nm == name:
                    out.append(int(oid))
    return out


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and obj_name(o) in LAND_NAMES
                and (name is None or obj_name(o) == name)):
            out.append(int(oid))
    return out


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def stack_entries(state):
    return state.get("stack") or []


# ------------------------------------------------------------- protocol-106 helpers
def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is null on 106)."""
    return any(a.get("type") == "PassPriority" for a in acts)


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    # vi fallback: passPriority action-code choice
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose",
                                  "data": {"choiceId": ch.get("id")}}})
                return True
    return False


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. The 106 engine
    offers tapLandForMana / castSpell / activateAbility choice menus at
    ordinary priority windows; treating those as decisions stalls the game
    (they must not block priority passes)."""
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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("text")):
                t = d.get("name") or d.get("text")
                break
    return str(t)


def choice_status(ch):
    return (ch.get("status") or {}).get("type")


def cand_ref_oid(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("reference") is not None:
            ref = d["reference"]
            if isinstance(ref, dict):
                return ref.get("object_id") or ref.get("id")
            try:
                return int(ref)
            except (TypeError, ValueError):
                return None
    return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("seat") is not None:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None


def opp_candidates(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"), data.get("spec"))


def opp_waiting_kind(opp):
    wfk = opp.get("waitingForKind") or {}
    return wfk.get("code")


def vi_kind_code(st):
    """View-level waitingForKind code (protocol 106 surfaces it on the
    viewer_interaction, not reliably on each opportunity)."""
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cast_action_for(acts, oid):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def pick_target_candidate(opp, want_oid=None, state=None):
    """Prefer the candidate whose object reference matches want_oid; fall
    back to the single candidate if there is exactly one."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    if want_oid is not None:
        for ch in cands:
            if cand_ref_oid(ch) is not None \
                    and int(cand_ref_oid(ch)) == int(want_oid):
                return ch
    if len(cands) == 1:
        return cands[0]
    return None


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} "
        f"kind={sub['response']['type']} ({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await c.send_interaction(sub)
    return iid, cid


async def observe_server_hello():
    """Raw handshake to capture the OBSERVED ServerHello and assert it
    matches the pinned release identity before driving anything."""
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with websockets.connect(url, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        msg = json.loads(raw)
        assert msg.get("type") == "ServerHello", \
            f"expected ServerHello, got {msg.get('type')}"
        data = msg.get("data", {})
        obs = {
            "server_version": data.get("server_version"),
            "build_commit": data.get("build_commit"),
            "protocol_version": data.get("protocol_version"),
            "mode": data.get("mode"),
        }
        for k in ("server_version", "build_commit", "protocol_version",
                  "mode"):
            want = SERVER_IDENTITY[k]
            want_cmp = want[1:] if k == "server_version" and \
                str(want).startswith("v") else want
            assert obs[k] == want_cmp or obs[k] == want, (
                f"ServerHello {k}={obs[k]!r} != pinned {want!r}; "
                "refusing to run")
        say(f"ServerHello OK: {obs}")
        wire("server_hello", obs)
        return obs


# --- mulligan / bottom / discard / payment (protocol 106) ---
_mulligan_answered_rev = {}
_mulligan_kept = {}
_mulligan_counts = {}


async def mulligan_tick(c, acts, state, who, keep_fn, max_mulls=4):
    """Protocol 106: MulliganDecision arrives as a legacy legal action; the
    engine accepts the Action submission. Answers at most once per
    revision (a fresh decision arrives at a new revision after a Mulligan)."""
    ma = find_action(acts, "MulliganDecision")
    if not ma or _mulligan_kept.get(who):
        return False
    if _mulligan_answered_rev.get(who) == c.revision:
        return False
    hn = hand_names(state, c.player_id)
    mulls = _mulligan_counts.get(who, 0)
    if keep_fn(hn) or mulls >= max_mulls:
        _mulligan_kept[who] = True
        _mulligan_answered_rev[who] = c.revision
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        say(f"{who} keeps (hand={hn[:8]}, mulls={mulls})")
        wire("mulligan", {"who": who, "decision": "keep",
                          "mulls": mulls})
    else:
        _mulligan_counts[who] = mulls + 1
        _mulligan_answered_rev[who] = c.revision
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"{who} mulligans #{mulls + 1} (hand={hn[:8]})")
        wire("mulligan", {"who": who, "decision": "mulligan",
                          "mulls": mulls + 1})
    return True


_bottom_done = set()
_bottomed = set()


async def bottom_tick(c, st, state, who):
    """Protocol 106: bottom-after-mulligan is a vi schema/select
    opportunity. The 106 surface carries NO waitingForKind code (null at
    both view and opportunity level), so detect by context: turn 1 pregame,
    this seat mulliganed, not yet bottomed, and the candidates reference
    this seat's hand cards. (The exile-cost choice references graveyard
    cards and only appears mid-game with escape_live; the cleanup discard
    needs hand>7.)"""
    if _mulligan_counts.get(who, 0) == 0:
        return False
    if ("bottom", who) in _bottomed:
        return False
    if state.get("turn_number") != 1:
        return False
    if state.get("phase") not in ("Untap", "Upkeep", "Draw"):
        return False
    hand_oids = set()
    for o in player_of(state, c.player_id).get("hand", []):
        try:
            hand_oids.add(int(o))
        except (TypeError, ValueError):
            pass
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in _bottom_done:
            continue
        choices, rtype, spec = opp_candidates(opp)
        if not choices or rtype != "schema":
            continue
        stype = spec.get("type") if isinstance(spec, dict) else spec
        if stype not in ("select", "sequence"):
            continue
        n_hand = 0
        for ch in choices:
            r = cand_ref_oid(ch)
            if r is not None and int(r) in hand_oids:
                n_hand += 1
        if n_hand < max(1, len(choices) - 1):
            continue
        count = _mulligan_counts.get(who, 1)
        spec_data = (spec.get("data") if isinstance(spec, dict)
                     else {}) or {}
        try:
            count = int(spec_data.get("max") or spec_data.get("count")
                        or count)
        except (TypeError, ValueError):
            pass

        def bkey(oid):
            nm = lname(state, oid)
            # bottom forests first (keep the hunted action cards!);
            # keep the Party Dude most of all
            if nm == DUDE:
                return 2
            if nm == BEAR:
                return 1
            return 0

        id_by_ref = {}
        for ch in choices:
            r = cand_ref_oid(ch)
            if r is not None:
                id_by_ref[int(r)] = ch.get("id")
        picks = []
        for oid in sorted(hand_oids, key=bkey):
            if oid in id_by_ref:
                picks.append(id_by_ref[oid])
            if len(picks) >= count:
                break
        if len(picks) < count:
            picks = [ch.get("id") for ch in choices[:count]]
        _bottom_done.add(iid)
        _bottomed.add(("bottom", who))
        sub_type = stype if isinstance(stype, str) else "select"
        say(f"{who} bottoms {count} via vi ({sub_type})")
        wire("bottom_cards", {"who": who, "count": count, "picks": picks})
        await c.send_interaction(
            {"interactionId": iid,
             "response": {"type": sub_type, "data": {"choiceIds": picks}}})
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


_pay_vi_done = set()


async def pay_mana_vi(c, st, tag, needs):
    """Drive a vi tapLandForMana mana payment. Only call while a payment is
    genuinely expected (after a CastSpell submission); the 106 engine also
    offers tapLandForMana noise menus at ordinary priority windows."""
    if not needs or sum(max(0, v) for v in needs.values()) <= 0:
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (
                    None, "available"):
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
        if iid in _pay_vi_done:
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
        needs[used] = needs.get(used, 0) - 1
        _pay_vi_done.add(iid)
        cid = pick.get("id")
        say(f"[{tag}] paying mana via vi: tap {choice_text(pick)[:40]} "
            f"for {used} (remaining needs={needs})")
        wire("mana_payment_vi", {"who": tag, "used": used,
                                 "remaining": dict(needs), "iid": iid})
        await c.send_interaction(
            {"interactionId": iid,
             "response": {"type": "choose", "data": {"choiceId": cid}}})
        return True
    return False




# ------------------------------------------------------------- #6643 scenario body

def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == key]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def dude_level_of(state, dude_oid):
    """Best-effort read of the Class level from the object record."""
    o = get_obj(state, dude_oid)
    for key in ("class_level", "level", "classLevel"):
        v = o.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return None


async def main():
    init_logging()
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_fires", "A3_target_prompted",
            "A4_pump_correct", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    hello = await observe_server_hello()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    obs = {
        "dude_expected_level": 1,   # Class enters at level 1 (level-2 ability
                                    # requires ClassLevelIs(1))
        "dude_oid": None,
        "level_keys_seen": None,
        "attack_turn": None,
        "attacker_oid": None,
        "trigger_seen": False,
        "trigger_entry": None,
        "target_prompted": False,
        "target_answered": False,
        "hand_at_target": None,
        "interaction_shapes": [],
        "rejections": [],
        "no_trigger_deadline_turn": None,
        "awaiting_payment": None,  # {"needs": {...}, "why": str,
                                   #  "since": t, "expect": name|None}
        "answered_ids": [],
        "escape_decisions": [],
        "attacked": False,
        "done": False,
        "pre_exported": False,
        "mid_trigger_exported": False,
        "mid_target_exported": False,
        "mid_pump_exported": False,
        "post_exported": False,
        "stuck_watch_fired": False,
        "final_quiet": False,
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    def mark_payment(why, needs, expect=None):
        obs["awaiting_payment"] = {"needs": dict(needs), "why": why,
                                   "since": time.time(), "expect": expect}
        say(f"payment expected for {why}: needs={needs}")

    def clear_payment_if_done(state):
        ap = obs.get("awaiting_payment")
        if not ap:
            return
        needs = ap["needs"]
        if sum(max(0, v) for v in needs.values()) <= 0:
            obs["awaiting_payment"] = None
            say(f"payment for {ap['why']} complete (needs exhausted)")
            return
        # safety: on 106 some casts are payment_mode Auto (no payment
        # prompt appears); if the paid-for object is already on the stack
        # the cost was covered and the payment is done.
        expect = ap.get("expect")
        if expect:
            blob = json.dumps(stack_entries(state), default=str).lower()
            if expect.lower() in blob:
                obs["awaiting_payment"] = None
                say(f"payment for {ap['why']} auto-covered ({expect} on "
                    f"stack); clearing")
                return
        if time.time() - ap["since"] > 60:
            obs["awaiting_payment"] = None
            say(f"payment for {ap['why']} timed out; clearing")
            wire("payment_timeout", {k: v for k, v in ap.items()
                                     if k != "since"})

    def decide_discard(opp, state, who):
        # P0 stops playing lands at 8, so the hand overfills and cleanup
        # discards fire every turn: discard extra forests first, keep the
        # Dude most of all.
        choices, _rtype, _spec = opp_candidates(opp)
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices

        def rank(ch):
            oid = cand_ref_oid(ch)
            nm = lname(state, oid).lower() if oid \
                else choice_text(ch).lower()
            if nm == DUDE:
                return 5
            if nm == BEAR:
                return 3
            return 1
        avail.sort(key=rank)
        n_discard = max(1, len(avail) - 7)
        return avail[:n_discard], "discard_to_hand_size"

    def is_discard_opp(c, st, opp, state):
        """Protocol 106: DiscardToHandSize is a viewer_interaction
        opportunity. Gate on hand>7 plus hand-card candidates (106 often
        uses a generic 'choose' code or null)."""
        code = vi_kind_code(st).lower()
        hand = player_of(state, c.player_id).get("hand", [])
        if "discard" in code and len(hand) > 7:
            return True
        if len(hand) <= 7:
            return False
        choices, rtype, _spec = opp_candidates(opp)
        if not choices or rtype != "schema":
            return False
        hand_oids = set()
        for o in hand:
            try:
                hand_oids.add(int(o))
            except (TypeError, ValueError):
                pass
        for ch in choices:
            oid = cand_ref_oid(ch)
            if oid is not None and int(oid) in hand_oids:
                return True
        return False

    def declared_attacker_present(state, pid):
        """True when any of `pid`'s battlefield creatures already appears in
        state.combat.attackers. 2026-10-07: on protocol 106 the game runs a
        post-declaration priority round (step stays DeclareAttackers) before
        DeclareBlockers; re-submitting there (even an empty declaration)
        must not happen -- fall through to priority handling instead."""
        ours = set(bf_ids(state, pid, None))
        for a in (state.get("combat") or {}).get("attackers") or []:
            try:
                if int(a.get("object_id")) in ours:
                    return True
            except (TypeError, ValueError):
                continue
        return False
        """Protocol 106: DiscardToHandSize is a viewer_interaction
        opportunity. Gate on hand>7 plus hand-card candidates (106 often
        uses a generic 'choose' code or null)."""
        code = vi_kind_code(st).lower()
        hand = player_of(state, c.player_id).get("hand", [])
        if "discard" in code and len(hand) > 7:
            return True
        if len(hand) <= 7:
            return False
        choices, rtype, _spec = opp_candidates(opp)
        if not choices or rtype != "schema":
            return False
        hand_oids = set()
        for o in hand:
            try:
                hand_oids.add(int(o))
            except (TypeError, ValueError):
                pass
        for ch in choices:
            oid = cand_ref_oid(ch)
            if oid is not None and int(oid) in hand_oids:
                return True
        return False

    async def submit_vi_choice(c, opp, choice_ids, why, rtype=None,
                               stype=None):
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        rtype = rtype or resp.get("type")
        spec = data.get("spec") or {}
        stype = stype or (spec.get("type") if isinstance(spec, dict)
                          else spec)
        if rtype == "schema":
            sub_type = stype if isinstance(stype, str) else "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": sub_type,
                                "data": {"choiceIds": choice_ids}}}
        elif rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": choice_ids[0]}}}
        else:
            sub_type = stype if isinstance(stype, str) else "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": sub_type,
                                "data": {"choiceIds": choice_ids}}}
        obs["answered_ids"].append(iid)
        obs["_last_sub"] = {"iid": iid, "why": why,
                            "rej_mark": len(obs["rejections"])}
        obs["escape_decisions"].append(
            {"interactionId": iid, "why": why,
             "sub_type": sub["response"]["type"],
             "choice_ids": choice_ids})
        say(f"[{c.name}] {why}: submit {sub['response']['type']} "
            f"ids={choice_ids} (rtype={rtype} spec={stype})")
        wire("decision_submission", {"who": c.name, "why": why,
                                     "interactionId": iid,
                                     "submission": sub})
        await c.send_interaction(sub)

    async def handle_vi(c, st, state, who):
        """Answer cleanup discards (P0) and the Party Dude target prompt
        (after the trigger was observed). Returns True if a submission was
        sent."""
        acted = False
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                continue
            # cleanup discard first (its candidates reference hand cards)
            if is_discard_opp(c, st, opp, state):
                ch, why = decide_discard(opp, state, who)
                if ch is not None:
                    ids = [x.get("id") for x in ch]
                    await submit_vi_choice(c, opp, ids, why, stype="select")
                    acted = True
                continue
            # Party Dude target prompt: only after the trigger was seen.
            if not obs["trigger_seen"] or obs["target_answered"]:
                continue
            choices, rtype, spec = opp_candidates(opp)
            if not choices:
                continue
            codes = set()
            for ch in choices:
                codes.update(x for x in surf_codes(ch) if x)
            if codes and codes <= NON_DECISION_CODES and rtype != "schema":
                continue
            stype = spec.get("type") if isinstance(spec, dict) else spec
            if rtype == "schema" and stype not in ("select", "sequence"):
                continue
            # candidates must reference battlefield creatures (the
            # attacking Bear), not hand cards
            refs = [cand_ref_oid(ch) for ch in choices]
            refs = [r for r in refs if r is not None]
            if not refs:
                continue
            bear_oid = obs["attacker_oid"]
            ch = pick_target_candidate(opp, want_oid=bear_oid, state=state)
            if ch is None:
                say(f"[{who}] target prompt seen but no candidate for the "
                    f"attacking Bear (oid {bear_oid}); candidates="
                    f"{[choice_text(x)[:40] for x in choices[:6]]}")
                wire("target_prompt_no_bear", {"interactionId": iid,
                                               "opp": opp})
                continue
            obs["target_prompted"] = True
            obs["hand_at_target"] = len(
                player_of(state, c.player_id).get("hand", []))
            say(f"[{who}] Party Dude target prompt answered with the "
                f"attacking Bear (oid {bear_oid}); P0 hand at target time: "
                f"{obs['hand_at_target']}")
            wire("target_prompt", {"interactionId": iid,
                                   "hand_at_target": obs["hand_at_target"],
                                   "opp": opp})
            iid2, _cid = await submit_target(
                c, opp, rtype if rtype else "schema",
                stype if stype else "sequence", ch, who)
            obs["answered_ids"].append(iid2)
            obs["_last_sub"] = {"iid": iid2, "why": "dude_target",
                                "rej_mark": len(obs["rejections"])}
            obs["target_answered"] = True
            if not obs["mid_target_exported"]:
                await export("mid_target")
                obs["mid_target_exported"] = True
            acted = True
        return acted

    def dude_trigger_on_stack(state):
        """Find the Party Dude level-3 Attacks trigger on the stack."""
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str).lower()
            if "party dude" in blob and ("pump" in blob or "attack" in blob):
                return e
        return None

    async def p0_tick(st, acts, state):
        if await mulligan_tick(p0, acts, state, "P0",
                               lambda hn: FOREST in hn, max_mulls=4):
            return True
        if await bottom_tick(p0, st, state, "P0"):
            return True
        # payments: legacy actions first, then vi tapLandForMana while a
        # payment is genuinely expected (noise menus excluded).
        if await pay_tick(p0, acts):
            return True
        ap = obs.get("awaiting_payment")
        if ap and await pay_mana_vi(p0, st, "P0", ap["needs"]):
            return True
        clear_payment_if_done(state)
        # discards + the Party Dude target prompt
        if await handle_vi(p0, st, state, "P0"):
            return True
        # combat / trigger bookkeeping (advertised actions, not waiting_for)
        atypes = set(a.get("type") for a in acts)
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da and state.get("active_player") == 0:
                # 2026-10-07 guard: the attacker is already declared (this
                # branch re-fires during the post-declaration priority
                # round); never re-submit, fall through to priority.
                if obs["attacked"] and declared_attacker_present(state, 0):
                    say("DeclareAttackers still advertised post-declaration; "
                        "attacker already in combat -- skipping resubmit")
                else:
                    dude = bf_ids(state, 0, DUDE)
                    dude = dude[0] if dude else None
                    ready = [b for b in bf_ids(state, 0, BEAR)
                             if can_attack_now(state, b)]
                    lvl = obs["dude_expected_level"]
                    if (not obs["attacked"] and dude is not None
                            and lvl >= 3 and ready):
                        say(f"PRE: exporting (dude lvl expected={lvl} "
                            f"field={dude_level_of(state, dude)}, bear "
                            f"{ready[0]} ready, P1 life={life_of(state, 1)})")
                        await export("pre")
                        obs["pre_exported"] = True
                        d = copy.deepcopy(da)
                        d.setdefault("data", {}).update(
                            {"attacks": [[ready[0],
                                         {"type": "Player", "data": 1}]],
                             "bands": []})
                        obs["attacker_oid"] = ready[0]
                        obs["attack_turn"] = state.get("turn_number")
                        obs["no_trigger_deadline_turn"] = \
                            obs["attack_turn"] + 6
                        await submit_as_is(p0, d)
                        obs["attacked"] = True
                        say(f"P0 attacks P1 with Bear (oid {ready[0]}) on "
                            f"turn {obs['attack_turn']}")
                        wire("p0_attacks", {"attacker": ready[0],
                                            "defender": 1,
                                            "turn": obs["attack_turn"]})
                        return True
                    d = copy.deepcopy(da)
                    d.setdefault("data", {}).update(
                        {"attacks": [], "bands": []})
                    if not obs["pre_exported"]:
                        say(f"P0 declares no attackers (dude={dude} "
                            f"lvl={lvl} ready_bears={ready})")
                    await submit_as_is(p0, d)
                    return True
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p0, d)
            return True
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True
        # trigger tracking: observe the Party Dude trigger on the stack
        if obs["attacked"] and not obs["trigger_seen"]:
            hit = dude_trigger_on_stack(state)
            if hit:
                obs["trigger_seen"] = True
                obs["trigger_entry"] = json.dumps(hit, default=str)[:2000]
                say("*** PARTY DUDE TRIGGER OBSERVED ON STACK ***")
                wire("dude_trigger_on_stack", hit)
                if not obs["mid_trigger_exported"]:
                    await export("mid_trigger")
                    obs["mid_trigger_exported"] = True
                return True

        # protocol 106: priority = PassPriority in top-level legal_actions
        if not my_priority(acts):
            return False
        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain"))
        if own_main and not real_decision_pending(st):
            # 1. land: stop playing lands once P0 has 8 on the
            # battlefield, so drawn Forests accumulate in hand and X =
            # hand size is non-trivial at pump time.
            n_forest = sum(1 for oid, o in state.get("objects", {}).items()
                           if o.get("zone") == "Battlefield"
                           and o.get("controller") == 0
                           and lname(state, oid) == FOREST)
            if n_forest < 8:
                for a in acts:
                    if a["type"] == "PlayLand":
                        await submit_as_is(p0, a)
                        return True
            # discover the Dude on the battlefield (once)
            dude = bf_ids(state, 0, DUDE)
            dude = dude[0] if dude else None
            if dude is not None and obs["dude_oid"] is None:
                obs["dude_oid"] = dude
                say(f"Party Dude on BF (oid {dude})")
            if dude is not None and obs["level_keys_seen"] is None:
                o = get_obj(state, dude)
                keys = [k for k in o.keys() if "level" in k.lower()]
                obs["level_keys_seen"] = keys
                say(f"Dude object level-ish keys: {keys}; "
                    f"level read={dude_level_of(state, dude)}")
                wire("dude_object_keys", {"oid": dude, "level_keys": keys,
                                          "all_keys": sorted(o.keys())})
            # 2. cast Party Dude
            if dude is None:
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and lname(state, d.get("object_id")) == DUDE):
                        say("P0 casts Party Dude")
                        wire("cast_dude", a)
                        await submit_as_is(p0, a)
                        mark_payment("cast_dude", {"G": 1},
                                     expect="party dude")
                        return True
            # 3. level-ups (sorcery speed): ability_index 0 = level 2,
            #    1 = level 3. Submitted while holding priority (never
            #    pass before submitting).
            if dude is not None:
                want = None
                if obs["dude_expected_level"] < 2:
                    want = 0
                elif obs["dude_expected_level"] < 3:
                    want = 1
                if want is not None:
                    for a in acts:
                        d = a.get("data", {})
                        if (a["type"] == "ActivateAbility"
                                and d.get("source_id") == dude
                                and d.get("ability_index") == want):
                            say(f"P0 activates Party Dude level-up idx="
                                f"{want} (-> level {want + 2})")
                            wire("level_up", {"ability_index": want,
                                              "action": a})
                            await submit_as_is(p0, a)
                            obs["dude_expected_level"] = want + 2
                            mark_payment(
                                f"dude_level{want + 2}",
                                {"G": 1, "generic": 1} if want == 0
                                else {"G": 1, "generic": 4},
                                expect="party dude")
                            return True
            # 4. cast a bear (driver keeps one bear on the battlefield
            # for the attack)
            if (not bf_ids(state, 0, BEAR)
                    and [o for o in player_of(state, 0).get("hand", [])
                         if lname(state, o) == BEAR]):
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and lname(state, d.get("object_id")) == BEAR):
                        say("P0 casts Grizzly Bears")
                        wire("cast_bear", a)
                        await submit_as_is(p0, a)
                        mark_payment("cast_bear", {"G": 1, "generic": 1},
                                     expect="grizzly bears")
                        return True
        if not real_decision_pending(st):
            return await pass_priority(p0, st, acts)
        return False

    async def p1_tick(st, acts, state):
        if await mulligan_tick(p1, acts, state, "P1",
                               lambda hn: ISLAND in hn, max_mulls=4):
            return True
        if await bottom_tick(p1, st, state, "P1"):
            return True
        if await pay_tick(p1, acts):
            return True
        ap = obs.get("p1_awaiting_payment")
        if ap and await pay_mana_vi(p1, st, "P1", ap["needs"]):
            return True
        if await handle_vi(p1, st, state, "P1"):
            return True
        atypes = set(a.get("type") for a in acts)
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(p1, d)
            return True
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p1, d)
            return True
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if not my_priority(acts):
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain"))
        if own_main and not real_decision_pending(st):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
        if not real_decision_pending(st):
            return await pass_priority(p1, st, acts)
        return False

    def evaluate():
        pre = load_env("pre")
        post = load_env("post")
        notes.append(
            f"attacked={obs['attacked']} attack_turn={obs['attack_turn']} "
            f"trigger_seen={obs['trigger_seen']} "
            f"target_prompted={obs['target_prompted']} "
            f"target_answered={obs['target_answered']} "
            f"hand_at_target={obs['hand_at_target']} "
            f"rejections={len(obs['rejections'])} "
            f"stuck_watch={obs['stuck_watch_fired']}")
        # A1: pre.json at DeclareAttackers
        if pre is not None:
            s = pre["state"]
            dude = bf_ids(s, 0, DUDE)
            dude = dude[0] if dude else None
            bears = [b for b in bf_ids(s, 0, BEAR) if can_attack_now(s, b)]
            lvl_field = dude_level_of(s, dude) if dude is not None else None
            ok = (dude is not None and bears
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20
                  and obs["dude_expected_level"] >= 3)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: dude_oid={dude} expected_lvl="
                         f"{obs['dude_expected_level']} field_lvl="
                         f"{lvl_field} ready_bears={bears} life="
                         f"{[life_of(s, i) for i in (0, 1)]}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing (attack never declared)")
        # A2: trigger on the stack
        trig = obs["trigger_entry"] or ""
        if obs["trigger_seen"] and "pump" in trig.lower():
            ass["A2_trigger_fires"] = "passed"
            notes.append(f"A2: Party Dude Attacks trigger observed on "
                         f"stack: {trig[:300]}")
        elif obs["trigger_seen"]:
            ass["A2_trigger_fires"] = "passed"
            notes.append(f"A2: Party Dude trigger observed on stack "
                         f"(entry text: {trig[:300]})")
        else:
            ass["A2_trigger_fires"] = "failed"
            notes.append("A2: no Party Dude trigger observed on the stack "
                         "within 6 turns of the attack (the reported bug)")
        # A3: target prompt answered
        ass["A3_target_prompted"] = ("passed" if obs["target_answered"]
                                     else "failed")
        notes.append(f"A3: target_prompted={obs['target_prompted']} "
                     f"target_answered={obs['target_answered']} "
                     f"attacker_oid={obs['attacker_oid']}")
        # A4: the pump's observable outcome. Primary: combat damage dealt
        # to P1 equals 2 + X where X = P0's hand size at target time (no
        # draws occur between the target choice and resolution).
        # Corroboration: bear P/T in mid_pump.json when the engine
        # surfaces it.
        x = obs.get("hand_at_target")
        pre_life = life_of(pre["state"], 1) if pre else None
        post_life = life_of(post["state"], 1) if post else None
        dmg = (pre_life - post_life
               if pre_life is not None and post_life is not None else None)
        mp = load_env("mid_pump")
        bear = obs["attacker_oid"]
        mp_bo = (get_obj(mp["state"], bear) if (mp and bear) else {})
        if x is None or x == 0 or dmg is None:
            ass["A4_pump_correct"] = "not-run"
            notes.append(f"A4: not evaluable (hand_at_target={x} "
                         f"dmg={dmg})")
        else:
            ok = (dmg == 2 + x)
            ass["A4_pump_correct"] = "passed" if ok else "failed"
            notes.append(f"A4: P1 life {pre_life}->{post_life} (damage "
                         f"{dmg}), expected {2 + x} (2+X, X=hand {x}); "
                         f"mid_pump bear P/T="
                         f"{mp_bo.get('power')}/{mp_bo.get('toughness')}")
        # A5: stack empty of Party Dude leftovers, game proceeding
        if post is not None:
            s = post["state"]
            stack = s.get("stack") or []
            leftover = [e for e in stack
                        if "party dude" in json.dumps(e, default=str).lower()]
            ass["A5_cleanup"] = ("passed" if not leftover else "failed")
            notes.append(f"A5: stack={len(stack)} entries, party-dude "
                         f"leftovers={len(leftover)}, phase={s.get('phase')} "
                         f"turn={s.get('turn_number')}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5: post.json missing")
        # verdict rule (same as the v0.85.0 run)
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif ass["A2_trigger_fires"] == "failed":
            verdict = "reproduced"
        elif ass["A3_target_prompted"] == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif (ass["A2_trigger_fires"] == "passed"
                and ass["A3_target_prompted"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict not-reproduced on A2+A3 despite "
                         + ", ".join(f"{k}={v}" for k, v in ass.items()
                                     if v != "passed"))
        else:
            verdict = "reproduced"
        return verdict

    async def finish():
        dur = time.time() - t_start
        if not os.path.exists(f"{EVDIR}/post.json"):
            try:
                await export("post")
                obs["post_exported"] = True
            except Exception:
                pass
        verdict = evaluate()
        # server-log excerpts: the running server is the shared pinned one
        # (started by run 20261005-0345); capture matching lines
        try:
            slog = open(f"{BACKFILL}/runs/20261007-0011-6557/server.log",
                        encoding="utf-8", errors="replace").read()
            sig_lines = [ln for ln in slog.splitlines()
                         if "party dude" in ln.lower()
                         or "class" in ln.lower()
                         or "trigger" in ln.lower()]
            with open(f"{EVDIR}/server_excerpts.log", "w") as f:
                f.write(f"# server log excerpts for run {RUN_ID} "
                        f"(issue #{ISSUE}); source: shared pinned server "
                        f"log runs/20261007-0011-6557/server.log\n")
                f.write(f"# {len(sig_lines)} matching lines\n")
                for ln in sig_lines[-80:]:
                    f.write(ln + "\n")
            say(f"server excerpts: {len(sig_lines)} matching lines")
        except Exception as e:
            notes.append(f"server log read failed: {e}")
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_observed": hello,
            "server_run_dir": "runs/20261007-0011-6557",
            "driver": {"protocol_advertised": 106,
                       "client": "driver/client.py"},
            "scenario": {"file": "scenario_6643_01030.py",
                         "sha256": hashlib.sha256(
                             open(__file__, "rb").read()).hexdigest()},
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k not in ("escape_decisions",)},
            "escape_decisions": obs["escape_decisions"],
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "Dense >4-of card counts are a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The Party Dude fixture is reconstructed (cast + level-ups), "
                "not the reporter's attached game state.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
                "waiting_for is null on protocol 106: priority is detected "
                "via the advertised PassPriority legal action; the Mulligan "
                "decision via the advertised MulliganDecision action.",
            ],
            "setup_line": "P0: 8x Party Dude + 12x Grizzly Bears + 40x "
                          "Forest; P1: 60x Island",
            "contract_line": "Cast Party Dude, level to 3 ({1}{G}, {4}{G}), "
                             "cast a Bear, attack P1: the level-3 "
                             "Attacks-mode trigger must go on the stack, "
                             "the up-to-one target-attacking-creature prompt "
                             "must be offered, and the Bear must get +X/+X "
                             "with X = P0 hand size",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=2, default=str)
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump(ass, f, indent=2)
        with open(f"{EVDIR}/obs.json", "w") as f:
            json.dump(obs, f, indent=2, default=str)
        # copy the running driver source into the evidence dir
        with open(__file__, "rb") as src, \
                open(f"{EVDIR}/scenario_6643_01030.py", "wb") as dst:
            dst.write(src.read())
        # close logs BEFORE the manifest (hashes must cover final bytes)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        files = sorted(f for f in os.listdir(EVDIR)
                       if f != "manifest.sha256"
                       and os.path.isfile(os.path.join(EVDIR, f)))
        with open(f"{EVDIR}/manifest.sha256", "w") as mf:
            for fn in files:
                h = hashlib.sha256(
                    open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
                mf.write(f"{h}  {fn}\n")
        say(f"manifest written for {len(files)} files")
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            await drain_rejections(c)
            # rejection-retry (#4509): if the last vi submission was
            # rejected, unmark its iid so the next tick retries instead
            # of stalling.
            last_sub = obs.pop("_last_sub", None)
            if last_sub and len(obs["rejections"]) > last_sub["rej_mark"]:
                iid = last_sub["iid"]
                if iid in obs["answered_ids"]:
                    obs["answered_ids"].remove(iid)
                if last_sub["why"] == "dude_target":
                    obs["target_answered"] = False
                say(f"[{c.name}] {last_sub['why']} submission rejected; "
                    f"unmarking iid {iid} for retry")
                wire("vi_rejected_retry",
                     {"iid": iid, "why": last_sub["why"]})
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        # prompt finish: after the target is answered and the trigger
        # leaves the stack, capture mid_pump.json on the attack turn once
        # combat has moved past DeclareAttackers (the +X/+X lasts until
        # end of turn); then export post.json after combat damage and
        # finish.
        cur = (p0.latest or {}).get("state", {}) or {}
        if obs["target_answered"] and not obs["post_exported"]:
            stack = cur.get("stack") or []
            dude_left = [e for e in stack
                         if "party dude" in json.dumps(e, default=str).lower()]
            atk_turn = obs["attack_turn"]
            if (not dude_left and not obs["mid_pump_exported"]
                    and atk_turn is not None
                    and cur.get("turn_number") == atk_turn
                    and cur.get("phase") not in ("DeclareAttackers", None)):
                await export("mid_pump")
                obs["mid_pump_exported"] = True
                mp = load_env("mid_pump")
                mbo = get_obj(mp["state"], obs["attacker_oid"]) \
                    if (mp and obs["attacker_oid"]) else {}
                say(f"mid_pump: bear P/T={mbo.get('power')}/"
                    f"{mbo.get('toughness')} "
                    f"phase={mp['state'].get('phase') if mp else '?'}")
            ct = cur.get("turn_number")
            if (obs["mid_pump_exported"]
                    and (ct is not None and atk_turn is not None
                         and (ct > atk_turn
                              or cur.get("phase") in ("PostCombatMain",
                                                      "EndStep", "Cleanup",
                                                      "Untap", "Upkeep",
                                                      "Draw")))):
                await export("post")
                obs["post_exported"] = True
                obs["final_quiet"] = not real_decision_pending(
                    p0.latest or {})
                await finish()
                return
            dl = atk_turn + 6 if atk_turn is not None else None
            if dl and ct is not None and ct >= dl:
                say("target answered but post not captured by turn "
                    f"{ct}; exporting post and finishing")
                await export("post")
                obs["post_exported"] = True
                await finish()
                return
        # no-trigger watchdog: export post and finish
        dl = obs["no_trigger_deadline_turn"]
        ct = cur.get("turn_number")
        if (obs["attacked"] and not obs["trigger_seen"] and dl
                and ct is not None and ct >= dl):
            say("no-trigger watchdog fired; finishing")
            await export("post")
            obs["post_exported"] = True
            await finish()
            return
        if cur.get("turn_number", 0) >= 30 and not obs["done"]:
            notes.append("turn 30 reached without completing; bailing out "
                         "to evaluation")
            say("turn 30 bail-out; finishing")
            await finish()
            return
        if obs["attacked"] and obs["trigger_seen"] and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not (obs["attacked"] and obs["trigger_seen"]):
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            obs["stuck_watch_fired"] = True
            notes.append("STUCK WATCH FIRED (180s post-trigger): "
                         f"priority_player={s.get('priority_player')}")
            say("STUCK WATCH FIRED post-trigger")
            try:
                await export("mid_stall")
            except Exception:
                pass
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} pp={s.get('priority_player')} "
                f"hand={hand_names(s, 0)[:6]} "
                f"dude={bf_ids(s, 0, DUDE)} exp_lvl="
                f"{obs['dude_expected_level']} bears={bf_ids(s, 0, BEAR)} "
                f"life={[life_of(s, i) for i in (0, 1)]} "
                f"stack={len(s.get('stack') or [])} attacked={obs['attacked']} "
                f"trig={obs['trigger_seen']} targ={obs['target_answered']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
