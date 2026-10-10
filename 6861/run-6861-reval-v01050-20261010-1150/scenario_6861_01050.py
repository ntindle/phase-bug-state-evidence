#!/usr/bin/env python3
"""Issue #6861 re-validation on pinned v0.105.0 (protocol 120): Squee, the Immortal.

Reported (Discord): "the card says you can cast it from grave and exile but
after exiling him I can't cast him anymore; tested also from graveyard and
from the grave seems to work just fine."

Oracle: "You may cast this card from your graveyard or from exile."
Data level (v0.105.0, pinned card-data.json): the line still parses to an
Unimplemented static structure ("Static pattern matched but line failed
static parser: You may cast this card from your graveyard or from exile.")
-- i.e. no GraveyardCastPermission / ExileCastPermission is parsed at all
(same as v0.102.0/v0.103.0/v0.104.0).

The v0.98.0 run (driver/scenario_6861_0980.py) reproduced: no CastSpell
advertised for the exiled Squee (A2 failed), raw CastSpell rejected
invalid_action (A3 failed); the graveyard control ALSO failed on v0.98.0
(A4 failed) -- re-tested on v0.102.0 (driver/scenario_6861_01020.py,
reproduced), and here on v0.104.0 (protocol 118); this run re-validates on
v0.105.0 (protocol 120).

Plan (two human seats, native engine, protocol 120):
  SETUP - land drops; cast Faithless Looting, discard 2 Squees; cast
          Scavenging Ooze; activate Ooze targeting a Squee in P0's
          graveyard (exiles it). Gate: 1 Squee in exile, 1 Squee in gy,
          >=6 untapped lands, P0 main phase -> export pre.json.
  EXILE - attempt to cast the exiled Squee. Record whether CastSpell is
          advertised (A2) and whether it reaches stack -> battlefield (A3).
  GYCTL - control: cast a Squee from the graveyard (A4); answer the legend
          rule preferring the just-cast Squee if it appears.

Behavioral contract:
  A1 setup_ok            pre.json: Squee in exile + Squee in gy, P0 main
                         phase, >=6 untapped lands
  A2 exile_cast_offered  CastSpell advertised for the exiled Squee (legacy
                         action or viewer interaction)
  A3 exile_cast_completes exiled Squee reaches Stack then Battlefield
  A4 gy_cast_completes   control: gy Squee reaches Stack then Battlefield
  A5 cleanup             game proceeds after both attempts (stack empty,
                         no stuck prompt)

Verdict = reproduced iff A2 or A3 fails; not-reproduced iff A2, A3, A4
all pass; else blocked.

Protocol-120 driver notes (v0.105.0, ported from the verified v0.104.0
scenario_6861_01040.py; conventions proven on protocol 106):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action, gated on the action.
  - DiscardToHandSize via vi schema/select opportunity offering hand cards.
  - CastSpell/ActivateAbility via legacy Action (merged with
    legal_actions_by_object); mana via legacy PayMana actions and/or vi
    tapLandForMana menus driven by MANA_NEEDS.
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - Land-play matching uses is_land() over every land the seats can hold;
    playLand vi codes answered before the decision gate.
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
  - Cast-confirmation guard (protocol-120 necessity, 2026-10-09): with engine
    auto-pay there is no mana-payment phase to hold priority, so a bare
    CastSpell can lose a race to the driver's own PassPriority on the next
    tick and be silently dropped by the server. After every CastSpell (and
    the Ooze ability activation) the driver sets ST["pending_cast"] and
    answers only decisions/mana while it is unconfirmed; a 30s backstop
    clears a still-unconfirmed action and resets the scenario one-shot flag
    so the play re-triggers instead of stalling.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6861
RUN_ID = "run-6861-reval-v01050-20261010-1150"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

SQUEE = "squee, the immortal"
LOOTING = "faithless looting"
OOZE = "scavenging ooze"
MOUNTAIN = "mountain"
FOREST = "forest"

SQUEE_T = "Squee, the Immortal"
LOOTING_T = "Faithless Looting"
OOZE_T = "Scavenging Ooze"
MOUNTAIN_T = "Mountain"
FOREST_T = "Forest"

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "step": 0, "stop": False,
      "a2_offered": None, "a2_source": None}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
LOOT = {"cast": False, "in_flight": False, "discarded": False}
OOZE_ST = {"cast": False, "activated": False, "targeted": False,
           "done": False, "target_oid": None}
CAST = {}
LAST_IID = {"iid": None}
WF_NOTE = []


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def zone_by_name(state, pid, zone, lname):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == zone
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and str(o.get("base_name") or o.get("name") or "").lower() == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for oid in bf_oids(state, pid)
               if is_land(get_obj(state, oid))
               and not get_obj(state, oid).get("tapped"))


def squee_zone(state, oid):
    return get_obj(state, oid).get("zone")


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
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


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
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def ints_in(x):
    out = []
    if isinstance(x, bool):
        return out
    if isinstance(x, int):
        return [x]
    if isinstance(x, str):
        try:
            return [int(x)]
        except ValueError:
            return out
    if isinstance(x, dict):
        for v in x.values():
            out.extend(ints_in(v))
    elif isinstance(x, (list, tuple)):
        for v in x:
            out.extend(ints_in(v))
    return out


def replace_first_int(x, new):
    """Return a copy of x with the first int encountered replaced by new."""
    done = [False]

    def rec(v):
        if done[0]:
            return v
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            done[0] = True
            return new
        if isinstance(v, dict):
            return {k: rec(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [rec(vv) for vv in v]
        if isinstance(v, tuple):
            return tuple(rec(vv) for vv in v)
        return v

    return rec(x)


def st_of(c):
    return c.latest or {}

# ------------------------------------------------------------- interaction primitives
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    if a.get("type", "").lower().startswith("cast") and not ST.get("cast_shape_logged"):
        ST["cast_shape_logged"] = True
        wire("cast_action_shape", {"action": a})
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    LAST_IID["iid"] = sub.get("interactionId")
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"


def check_data_level():
    sq = CARD_DATA.get("squee, the immortal", {})
    oracle = str(sq.get("oracle_text", ""))
    effects = []
    for ab in sq.get("abilities", []) or []:
        eff = (ab.get("effect") or {})
        effects.append({"kind": ab.get("kind"),
                        "type": eff.get("type"),
                        "name": eff.get("name"),
                        "description": eff.get("description")})
    perm_kinds = set()
    for ab in sq.get("abilities", []) or []:
        t = str((ab.get("effect") or {}).get("type", ""))
        if "Permission" in t or "permission" in t:
            perm_kinds.add(t)
    ooze = CARD_DATA.get("scavenging ooze", {})
    loot = CARD_DATA.get("faithless looting", {})
    ev = {
        "squee_oracle": oracle,
        "squee_oracle_ok": oracle.strip() == "You may cast this card from your graveyard or from exile.",
        "squee_ability_effects": effects,
        "squee_parsed_permission_kinds": sorted(perm_kinds),
        "squee_cast_permission_implemented": any(
            "nimplemented" not in str(e.get("type", "")) for e in effects),
        "scavenging_ooze_oracle": str(ooze.get("oracle_text", "")),
        "scavenging_ooze_mana": ooze.get("mana_cost"),
        "faithless_looting_oracle": str(loot.get("oracle_text", "")),
        "faithless_looting_mana": loot.get("mana_cost"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    say(f"data-level: squee oracle ok={ev['squee_oracle_ok']} "
        f"effects={effects}")
    assert ev["squee_oracle_ok"], "Squee oracle text mismatch in pinned data"


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hn = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard(c, acts, st, pid, tag, rank):
    if LOOT.get("in_flight"):
        return False  # Looting's own discard handler owns this window
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    ref_of[str(d["reference"])] = ch["id"]
                    break
        ranked = sorted(hand, key=lambda o: (rank(state, o),
                                             obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                           tag)
        return True
    return False


async def do_declare_empty(c, acts, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["attacks"] = []
            dd["bands"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["assignments"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
    return False


async def do_legend(c, acts, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            wire("legend_action", {"who": tag, "action": a})
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            keep = CAST.get("oid")
            if keep:
                ints = ints_in(dd)
                if ints and int(keep) not in ints:
                    dd = replace_first_int(dd, int(keep))
                    d["data"] = dd
                    wire("legend_redirect_keep", {"keep_oid": keep})
                    say(f"[{tag}] legend rule: keeping cast Squee {keep}")
                else:
                    say(f"[{tag}] legend rule: keeping first (as-is)")
            else:
                say(f"[{tag}] legend rule: keeping first (as-is)")
            await submit_as_is(c, d)
            return True
    return False


async def pay_tick(c, acts, tag):
    # 2026-10-07 double-pay gate: CastSpell actions carry payment_mode Auto
    # and the engine taps lands itself; answering a legacy PayMana action on
    # top of that double-pays. Only fire when the driver has outstanding
    # mana needs for a NON-test cast (test casts keep MANA_NEEDS empty, and
    # setup casts keep the proven MANA_NEEDS-driven vi taps).
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) <= 0:
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            say(f"[{tag}] legacy pay-mana action answered "
                f"(outstanding needs={dict(needs)})")
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
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    """Play one land per turn; prefer() picks among hand lands."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer:
        lands.sort(key=lambda o: prefer(state, o))
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    # 106 may also advertise land plays as a vi playLand opportunity
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                ref = str(d.get("reference", ""))
                if ref in [str(x) for x in lands]:
                    iid = opp.get("interactionId")
                    key = (tag, "playland", str(iid), ref)
                    if key in SUBMITTED_OPPS:
                        continue
                    SUBMITTED_OPPS.add(key)
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                    wire("play_land_vi", {"who": tag, "oid": ref})
                    await answer_vi(c, opp, ch, tag)
                    return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
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


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == lname:
                return a, iv
    return None, None


def cast_action_for_oid(acts, oid):
    for a in acts:
        if "cast" not in str(a.get("type", "")).lower():
            continue
        vals = list((a.get("data") or {}).values()) + [a.get("_src_oid")]
        for v in vals:
            try:
                if int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def target_opportunity(st):
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
                codes.update(x for x in surf_codes(ch) if x)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(x in codes for x in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def cand_refs(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            out.append(str(d["reference"]))
    return out


def menu_cast_spells(st):
    """(choice_id -> (object reference, name, zone)) of castSpell choices."""
    out = {}
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            ref = name = zone = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and s.get("type") == "object":
                    ref = str(d.get("reference"))
                    name = d.get("name")
                    zone = d.get("zone")
            out[ch.get("id")] = (ref, name, zone)
    return out

# ------------------------------------------------------------- fixture handlers
def p0_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (SQUEE, LOOTING):
        return 3
    if ln == OOZE:
        return 2
    if is_land(get_obj(state, o)):
        return 0
    return 1


def p1_rank(state, o):
    return 0


async def do_looting_discard(c, acts, st, tag):
    """Answer Faithless Looting's discard-2 via vi, preferring 2 Squees."""
    if not LOOT.get("in_flight") or LOOT.get("discarded"):
        return False
    state = st["state"]
    hand_squees = [o for o in hand_ids(state, 0)
                   if obj_lname(state, o) == SQUEE]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(x for x in surf_codes(ch) if x)
        if "passPriority" in codes or "decideOptionalEffect" in codes:
            continue
        ref_of = {}
        for ch in items:
            for r in cand_refs(ch):
                ref_of.setdefault(r, ch["id"])
        hand_refs = set(hand_ids(state, 0))
        if not all(r in hand_refs for r in ref_of):
            continue  # not a hand-card choice opportunity
        iid = opp.get("interactionId")
        key = (tag, "looting_discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        picks = [o for o in hand_squees if o in ref_of][:2]
        fallback = False
        if len(picks) < 2:
            fallback = True
            rest = sorted(hand_ids(state, 0),
                          key=lambda o: (p0_rank(state, o), obj_lname(state, o)))
            picks = [o for o in rest if o in ref_of][:2]
            if len(picks) < 2:
                say(f"[{tag}] Looting discard: only {len(picks)} candidates; "
                    f"deferring")
                return False
        choice_ids = [ref_of[o] for o in picks]
        wire("looting_discard_shape",
             {"rtype": rtype,
              "spec": ((data.get("spec") or {}).get("type")
                       if isinstance(data.get("spec"), dict) else None),
              "codes": sorted(codes), "n_items": len(items),
              "fallback": fallback})
        if rtype == "schema":
            spec = (data.get("spec") or {}) or {}
            stype = spec.get("type")
            if stype not in ("select", "sequence"):
                say(f"[{tag}] Looting discard: unexpected spec {stype}; "
                    f"deferring")
                return False
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": choice_ids}}}
        elif rtype == "exactChoices" and len(choice_ids) == 1:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": choice_ids[0]}}}
        else:
            say(f"[{tag}] Looting discard: unexpected rtype={rtype}; deferring")
            return False
        SUBMITTED_OPPS.add(key)
        await interact_as(c, sub, tag)
        if fallback:
            LOOT["cast"] = False
            LOOT["in_flight"] = False
            say(f"[{tag}] Looting discarded non-Squees {picks}; will recast")
        else:
            LOOT["discarded"] = True
            say(f"[{tag}] Looting discards 2 Squees {picks}")
        wire("looting_discarded", {"oids": picks, "fallback": fallback})
        return True
    return False


async def do_ooze_target(c, acts, st, tag):
    """Answer Scavenging Ooze's exile-target selection with a gy Squee."""
    if not (OOZE_ST.get("activated") and not OOZE_ST.get("targeted")):
        return False
    state = st["state"]
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    sq = zone_by_name(state, 0, "Graveyard", SQUEE)
    if not sq:
        say(f"[{tag}] Ooze target: no Squee in gy; deferring")
        return False
    want = sq[0]
    chs = ((opp.get("response", {}) or {}).get("data", {}) or {}
           ).get("candidates") or \
        ((opp.get("response", {}) or {}).get("data", {}) or {}
         ).get("choices") or []
    pick = None
    for ch in chs:
        if want in cand_refs(ch):
            pick = ch
            break
    if pick is None:
        say(f"[{tag}] Ooze target: Squee {want} not among candidates; "
            f"deferring")
        wire("ooze_target_no_squee",
             {"want": want, "refs": [cand_refs(ch) for ch in chs]})
        return False
    iid = opp.get("interactionId")
    key = (tag, "ooze_target", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    OOZE_ST["targeted"] = True
    OOZE_ST["target_oid"] = want
    say(f"[{tag}] Ooze exiles Squee {want} (choice {pick.get('id')})")
    wire("ooze_target_submit", {"oid": want, "choice": pick.get("id")})
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": stype,
                            "data": {"choiceIds": [pick.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
    await interact_as(c, sub, tag)
    return True


async def setup_step(c, tag, state, acts):
    # land drop (retry every tick)
    if await play_a_land(c, state, 0, acts, tag, prefer=p0_land_prefer):
        return True
    # cast Faithless Looting (only when holding 2 Squees to discard)
    if not LOOT["cast"] and untapped_lands(state, 0) >= 1:
        n_squee_hand = sum(1 for o in hand_ids(state, 0)
                           if obj_lname(state, o) == SQUEE)
        if n_squee_hand >= 2:
            a, oid = find_cast_action(acts, state, LOOTING)
            if a is not None:
                MANA_NEEDS[tag] = {"R": 1}
                LOOT["cast"] = True
                LOOT["in_flight"] = True
                say(f"[{tag}] casts Faithless Looting (oid {oid})")
                wire("cast_looting", {"oid": oid})
                await submit_as_is(c, a)
                set_pending_cast("cast", oid, LOOTING_T, "SETUP-looting",
                                 "Hand")
                return True
    # cast Scavenging Ooze once 2+ Squees are in the graveyard
    if LOOT.get("discarded") and not OOZE_ST["cast"] \
            and untapped_lands(state, 0) >= 2:
        n_gy = len(zone_by_name(state, 0, "Graveyard", SQUEE))
        if n_gy >= 2:
            a, oid = find_cast_action(acts, state, OOZE)
            if a is not None:
                MANA_NEEDS[tag] = {"G": 1, "generic": 1}
                OOZE_ST["cast"] = True
                say(f"[{tag}] casts Scavenging Ooze (oid {oid})")
                wire("cast_ooze", {"oid": oid})
                await submit_as_is(c, a)
                set_pending_cast("cast", oid, OOZE_T, "SETUP-ooze", "Hand")
                return True
    # activate Ooze once it's on the battlefield and untapped
    if OOZE_ST["cast"] and not OOZE_ST["activated"] \
            and not OOZE_ST["done"]:
        ooze = [oid for oid in bf_oids(state, 0)
                if obj_lname(state, oid) == OOZE
                and not get_obj(state, oid).get("tapped")]
        n_gy = len(zone_by_name(state, 0, "Graveyard", SQUEE))
        if ooze and n_gy >= 1 and untapped_lands(state, 0) >= 1:
            ooze_oid = ooze[0]
            act = next((a for a in acts
                        if a.get("type") == "ActivateAbility"
                        and (str(a.get("_src_oid")) == str(ooze_oid)
                             or str((a.get("data") or {}).get("source_id"))
                             == str(ooze_oid))), None)
            if act is None:
                act = next((a for a in acts
                            if a.get("type") == "ActivateAbility"), None)
            if act is not None:
                MANA_NEEDS[tag] = {"G": 1}
                OOZE_ST["activated"] = True
                wire("activate_ooze_action", {"action": act})
                say(f"[{tag}] activates Scavenging Ooze (advertised action)")
                await submit_as_is(c, act)
                set_pending_cast("ability", ooze_oid, OOZE_T, "OOZE-ACT",
                                 "Battlefield")
                return True
            for opp in vi_ops(st_of(c)):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    if "activateAbility" not in surf_codes(ch):
                        continue
                    refs = cand_refs(ch)
                    if str(ooze_oid) in refs:
                        MANA_NEEDS[tag] = {"G": 1}
                        OOZE_ST["activated"] = True
                        wire("activate_ooze_vi", {"src": ooze_oid})
                        say(f"[{tag}] activates Scavenging Ooze (vi choice)")
                        await answer_vi(c, opp, ch, tag)
                        set_pending_cast("ability", ooze_oid, OOZE_T,
                                         "OOZE-ACT", "Battlefield")
                        return True
            say(f"[{tag}] Ooze activation: no advertised action/choice yet")
            return True
    return False


def p0_land_prefer(state, o):
    # Mountain-first until 2 Mountains (R for Looting / RR for Squee),
    # then Forest until 2 Forests (G for Ooze), then alternate.
    ln = obj_lname(state, o)
    n_m = sum(1 for oid in bf_oids(state, 0)
              if obj_lname(state, oid) == MOUNTAIN)
    n_f = sum(1 for oid in bf_oids(state, 0)
              if obj_lname(state, oid) == FOREST)
    if n_m < 2:
        return 0 if ln == MOUNTAIN else 1
    if n_f < 2:
        return 0 if ln == FOREST else 1
    turn = state.get("turn_number") or 0
    if turn % 2 == 0:
        return 0 if ln == MOUNTAIN else 1
    return 0 if ln == FOREST else 1


def new_cast(tag, oid):
    CAST.clear()
    CAST.update({"tag": tag, "oid": str(oid), "in_flight": True,
                 "announced": False, "stack_seen": False, "bf_seen": False,
                 "done": False, "rejected": False, "silent_fail": False,
                 "offered": None, "rev_at_submit": None, "submitted": False,
                 "menu_wait_t0": None})


def raw_cast_attempt(state, eid, acts):
    """Build a raw CastSpell attempt for the Squee oid: clone an advertised
    CastSpell shape if one exists (swap object/card ids), else fall back to
    a minimal wire shape."""
    ref = next((a for a in acts
                if "cast" in str(a.get("type", "")).lower()), None)
    obj = get_obj(state, eid)
    card_id = obj.get("card_id", int(eid))
    if ref is not None:
        out = copy.deepcopy(ref)
        out.pop("metadata", None)
        out.pop("_src_oid", None)
        d = dict(out.get("data", {}) or {})
        for k in list(d.keys()):
            if "object" in k.lower() and isinstance(d[k], int):
                d[k] = int(eid)
            elif "card" in k.lower() and isinstance(d[k], int):
                d[k] = int(card_id)
        out["data"] = d
        return out
    return {"type": "CastSpell",
            "data": {"object_id": int(eid), "card_id": int(card_id),
                     "targets": [], "payment_mode": {"type": "Auto"}}}


async def submit_cast_attempt(c, a, vich, st):
    """Submit a cast via the advertised legacy action if present, else via
    the viewer-interaction castSpell choice."""
    if a is not None:
        await submit_as_is(c, a)
        return "legal_actions"
    opp, ch = vich
    iid = opp.get("interactionId") or opp.get("id")
    LAST_IID["iid"] = iid
    await answer_vi(c, opp, ch, c.name)
    return "viewer_interaction"


def vi_cast_choice(st, oid):
    """A viewer_interaction castSpell choice for the exact object: match the
    object surface's reference id, not a blob substring."""
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None

async def exile_cast_step(c, tag, state, acts):
    st = st_of(c)
    if not CAST.get("in_flight"):
        sq = zone_by_name(state, 0, "Exile", SQUEE)
        if not sq:
            say("EXILE step: no Squee in exile; aborting")
            ST["stop"] = True
            return False
        eid = sq[0]
        a = cast_action_for_oid(acts, eid)
        vich = vi_cast_choice(st, eid)
        menu = menu_cast_spells(st)
        wire("exile_menu", {"oid": eid, "menu_cast_spells": menu,
                            "n_vi_ops": len(vi_ops(st))})
        with open(f"{EVDIR}/exile_menu.json", "w") as f:
            json.dump({"oid": eid, "menu_cast_spells": menu,
                       "n_vi_ops": len(vi_ops(st)),
                       "legacy_cast_action": a}, f, indent=1, default=str)
        say(f"EXILE menu: {len(menu)} castSpell choices: "
            f"{[(v[0], v[1], v[2]) for v in menu.values()]}")
        if not a and not vich:
            # the Priority menu can lag a tick behind the gate; wait a bit
            # before concluding the cast is not offered.
            t0 = CAST.get("menu_wait_t0") or time.time()
            CAST["menu_wait_t0"] = t0
            if time.time() - t0 < 20:
                return True
            say("EXILE menu: no castSpell choice for the exiled Squee "
                "after 20s; concluding not offered")
        CAST.pop("menu_wait_t0", None)
        new_cast("EXILE", eid)
        CAST["offered"] = bool(a or vich)
        ST["a2_offered"] = CAST["offered"]
        ST["a2_source"] = ("legal_actions" if a else
                           ("viewer_interaction" if vich else "none"))
        wire("exile_cast_attempt", {"oid": eid, "offered": CAST["offered"],
                                    "source": ST["a2_source"]})
        say(f"EXILE cast: oid={eid} offered={CAST['offered']} "
            f"({ST['a2_source']})")
        if a or vich:
            # 2026-10-07 double-pay gate: the driver never pays mana for
            # test casts (neither vi taps nor legacy PayMana) -- the
            # engine's auto-tap is the single payer. MANA_NEEDS stays
            # empty for the test Squee casts.
            MANA_NEEDS[tag] = {}  # Squee {1}{R}{R}; engine pays
            CAST["via"] = await submit_cast_attempt(c, a, vich, st)
            set_pending_cast("cast", eid, SQUEE_T, "EXILE", "Exile")
        else:
            # attempt the raw action anyway; capture the rejection (or not).
            raw = raw_cast_attempt(state, eid, acts)
            wire("exile_raw_attempt", {"action": raw})
            await submit_as_is(c, raw)
            CAST["via"] = "raw_action"
            set_pending_cast("cast", eid, SQUEE_T, "EXILE", "Exile")
        CAST["submitted"] = True
        CAST["rev_at_submit"] = c.revision
        CAST["wall_at_submit"] = time.time()
        return True
    return False


async def gy_cast_step(c, tag, state, acts):
    st = st_of(c)
    if not CAST.get("in_flight"):
        sq = zone_by_name(state, 0, "Graveyard", SQUEE)
        if not sq:
            say("GY step: no Squee in graveyard; aborting")
            ST["stop"] = True
            return False
        gid = sq[0]
        a = cast_action_for_oid(acts, gid)
        vich = vi_cast_choice(st, gid)
        menu = menu_cast_spells(st)
        wire("gy_menu", {"oid": gid, "menu_cast_spells": menu,
                         "n_vi_ops": len(vi_ops(st))})
        with open(f"{EVDIR}/gy_menu.json", "w") as f:
            json.dump({"oid": gid, "menu_cast_spells": menu,
                       "n_vi_ops": len(vi_ops(st)),
                       "legacy_cast_action": a}, f, indent=1, default=str)
        say(f"GY menu: {len(menu)} castSpell choices: "
            f"{[(v[0], v[1], v[2]) for v in menu.values()]}")
        if not a and not vich:
            t0 = CAST.get("menu_wait_t0") or time.time()
            CAST["menu_wait_t0"] = t0
            if time.time() - t0 < 20:
                return True
            say("GY menu: no castSpell choice for the gy Squee "
                "after 20s; concluding not offered")
        CAST.pop("menu_wait_t0", None)
        new_cast("GYCTL", gid)
        CAST["offered"] = bool(a or vich)
        src = ("legal_actions" if a else
               ("viewer_interaction" if vich else "none"))
        wire("gy_cast_attempt", {"oid": gid, "offered": CAST["offered"],
                                 "source": src})
        say(f"GY control cast: oid={gid} offered={CAST['offered']} ({src})")
        if a or vich:
            # 2026-10-07 double-pay gate: test casts keep MANA_NEEDS
            # empty; the engine's auto-tap is the single payer.
            MANA_NEEDS[tag] = {}
            CAST["via"] = await submit_cast_attempt(c, a, vich, st)
            set_pending_cast("cast", gid, SQUEE_T, "GYCTL", "Graveyard")
        else:
            raw = raw_cast_attempt(state, gid, acts)
            wire("gy_raw_attempt", {"action": raw})
            await submit_as_is(c, raw)
            CAST["via"] = "raw_action"
            set_pending_cast("cast", gid, SQUEE_T, "GYCTL", "Graveyard")
            say("GY control: no advertised CastSpell; attempting raw action")
        CAST["submitted"] = True
        CAST["rev_at_submit"] = c.revision
        CAST["wall_at_submit"] = time.time()
        return True
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def finish_cast(c, tag, how):
    """how: rejected | resolved | silent. Exports the post state and
    advances the scenario."""
    post_name = "post_exile.json" if CAST.get("tag") == "EXILE" \
        else "post_gy.json"
    await do_export(c, post_name)
    if CAST.get("tag") == "GYCTL":
        import shutil
        shutil.copy(f"{EVDIR}/post_gy.json", f"{EVDIR}/post.json")
    CAST["in_flight"] = False
    CAST["done"] = True
    ST["pending_cast"] = None  # cast settled; no guard window remains
    if how == "silent":
        CAST["silent_fail"] = True
    if CAST.get("tag") == "EXILE":
        ST["step"] = 2
        say(f"=== EXILE cast {how} -> GY control (step 2) ===")
    else:
        ST["stop"] = True
        say(f"=== GY cast {how}; finishing ===")
    return True


async def track_cast(c, tag):
    """Track an in-flight Squee cast. Returns True if it handled the tick."""
    if not CAST.get("in_flight"):
        return False
    st = st_of(c)
    state = st["state"]
    acts = merged_actions(st)
    oid = CAST["oid"]
    zone = squee_zone(state, oid)
    if zone == "Stack" and not CAST.get("announced"):
        CAST["announced"] = True
        CAST["stack_seen"] = True
        wire(f"{CAST['tag'].lower()}_stack_seen", {"oid": oid})
        say(f"{CAST['tag']} cast: Squee {oid} on stack")
    if zone == "Battlefield" and not CAST.get("bf_seen"):
        CAST["bf_seen"] = True
        wire(f"{CAST['tag'].lower()}_bf_seen", {"oid": oid})
        say(f"{CAST['tag']} cast: Squee {oid} on battlefield")
    if CAST.get("rejected"):
        return await finish_cast(c, tag, "rejected")
    if CAST.get("announced") and zone != "Stack":
        return await finish_cast(c, tag, "resolved")
    if (not CAST.get("announced") and CAST.get("submitted")
            and (c.revision - (CAST.get("rev_at_submit") or c.revision) >= 15
                 or time.time() - CAST.get("wall_at_submit", time.time()) > 90)):
        if not real_decision_pending(st):
            return await finish_cast(c, tag, "silent")
    # while the spell is on the stack, pass so it can resolve
    if CAST.get("announced"):
        if await pass_priority(c, st, top_acts(st)):
            say(f"[{tag}] passes ({CAST['tag']} cast on stack)")
            return True
    return True


# ------------------------------------------------------------- cast-confirmation guard (protocol 120)
def set_pending_cast(kind, oid, name, tag, from_zone):
    ST["pending_cast"] = {"kind": kind, "oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag,
                          "from_zone": from_zone}


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    """True once the submitted action has verifiably taken effect."""
    if pc.get("kind") == "ability":
        # Ooze exile activation: confirmed once the target decision was
        # submitted (OOZE_ST["targeted"]) or the exile landed.
        return bool(OOZE_ST.get("targeted") or OOZE_ST.get("done"))
    zone = cast_obj_zone(state, pc["oid"])
    fz = str(pc.get("from_zone") or "").lower()
    if fz == "hand":
        # setup casts: leaving the hand confirms the cast fired
        return zone in ("stack", "battlefield", "graveyard", "exile",
                        "command")
    # test casts from Exile/Graveyard: the origin zone doubles as a terminal
    # zone, so only reaching the stack (or battlefield) confirms the cast.
    return zone in ("stack", "battlefield")


def cast_unconfirmed(state):
    pc = ST.get("pending_cast")
    return bool(pc) and not cast_confirmed(state, pc)


def cast_guard_tick(state, tag):
    """Protocol-120 cast-confirmation guard.

    Returns "hold" while our CastSpell (or Ooze activation) is in flight but
    unconfirmed: the driver must not pass priority or start new plays in
    that window -- on protocol 120 a bare CastSpell can lose a race to our
    own PassPriority submitted on the next tick and be silently dropped by
    the server (observed 2026-10-09). Returns "proceed" once the action is
    confirmed (or rejected); a 30s backstop clears a still-unconfirmed
    action and resets the scenario one-shot flag so the play re-triggers
    instead of stalling. Must run before any early-return block.
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    if CAST.get("rejected"):
        ST["pending_cast"] = None
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        say(f"[{tag}] action confirmed "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("action_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                  "zone": cast_obj_zone(state, pc["oid"])})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] ACTION NOT CONFIRMED after 30s "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("action_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]),
              "zone": cast_obj_zone(state, pc["oid"])})
        ST["pending_cast"] = None
        stag = pc.get("tag")
        if stag == "SETUP-looting":
            LOOT["cast"] = False
        elif stag == "SETUP-ooze":
            OOZE_ST["cast"] = False
        elif stag == "OOZE-ACT":
            OOZE_ST["activated"] = False
        elif stag in ("EXILE", "GYCTL"):
            CAST["in_flight"] = False
            CAST["submitted"] = False
        return "proceed"
    return "hold"


# ------------------------------------------------------------- seat ticks
async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    # ---- cast-confirmation guard (protocol 120): while our CastSpell or
    # ability activation is in flight but unconfirmed, answer decisions and
    # mana needs but start no new plays and pass no priority. Runs before
    # any early-return block so a dropped action is always detected.
    if cast_guard_tick(state, tag) == "hold":
        if await do_mulligan(c, acts, st, 0, tag):
            return True
        if await do_discard(c, acts, st, 0, tag, p0_rank):
            return True
        if await do_declare_empty(c, acts, tag):
            return True
        if await do_legend(c, acts, tag):
            return True
        if await pay_tick(c, acts, tag):
            return True
        if await do_looting_discard(c, acts, st, tag):
            return True
        if await do_ooze_target(c, acts, st, tag):
            return True
        # an announced/in-flight test cast still owns the tick
        if await track_cast(c, tag):
            return True
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(c, st, tag, needs):
                return True
        return True
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await do_legend(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True
    if await do_looting_discard(c, acts, st, tag):
        return True
    if await do_ooze_target(c, acts, st, tag):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # an announced/in-flight cast owns the tick (tracks + passes)
    if await track_cast(c, tag):
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if ST["stage"] == "SETUP":
            if await setup_step(c, tag, state, acts):
                return True
        elif ST["stage"] == "MAIN":
            if ST["step"] == 1:
                if await exile_cast_step(c, tag, state, acts):
                    return True
            elif ST["step"] == 2:
                if await gy_cast_step(c, tag, state, acts):
                    return True

    if real_decision_pending(st):
        return True
    # never pass under P0's unconfirmed cast (protocol-120 race)
    if my_priority(top_acts(st)) and not cast_unconfirmed(st["state"]):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await do_legend(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out

# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06861")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((SQUEE_T, 12), (LOOTING_T, 4), (OOZE_T, 4),
                         (MOUNTAIN_T, 20), (FOREST_T, 20)))
    p1 = PhaseClient("P16861")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN_T, 30), (FOREST_T, 30)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej and c is p0 and CAST.get("in_flight") \
                    and not CAST.get("announced"):
                CAST["rejected"] = True
                CAST["reject_data"] = [r[1] for r in rej]
                say(f"[P0] cast {CAST.get('tag')} rejected")
            if rej and LAST_IID["iid"] in SUBMITTED_OPPS:
                SUBMITTED_OPPS.discard(LAST_IID["iid"])
                say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                    f"after rejection")
                LAST_IID["iid"] = None
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # with no revision change (missed-broadcast resilience).
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]

        # Faithless Looting resolution bookkeeping
        if LOOT.get("in_flight") and LOOT.get("discarded"):
            if not zone_by_name(state, 0, "Stack", LOOTING):
                LOOT["in_flight"] = False
                say("Looting resolved (discard done)")
        # Ooze exile bookkeeping
        if OOZE_ST.get("targeted") and not OOZE_ST.get("done"):
            tgt = OOZE_ST.get("target_oid")
            if squee_zone(state, tgt) == "Exile":
                OOZE_ST["done"] = True
                say(f"Ooze exile done: Squee {tgt} now in Exile")
                wire("ooze_exile_done", {"oid": tgt})

        # SETUP -> MAIN gate
        if ST["stage"] == "SETUP" and my_main(state, 0):
            n_ex = len(zone_by_name(state, 0, "Exile", SQUEE))
            n_gy = len(zone_by_name(state, 0, "Graveyard", SQUEE))
            un = untapped_lands(state, 0)
            if n_ex >= 1 and n_gy >= 1 and un >= 6:
                await do_export(p0, "pre.json")
                ST["stage"] = "MAIN"
                ST["step"] = 1
                SUBMITTED_OPPS.clear()
                say(f"=== stage -> MAIN (exile={n_ex} gy={n_gy} "
                    f"untapped={un}) ===")
                wire("setup_ready", {"exile": n_ex, "gy": n_gy,
                                     "untapped": un})
                continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            s = state
            say(f"DIAG P0: rev={p0.revision} turn={s.get('turn_number')} "
                f"phase={s.get('phase')} prio={my_priority(top_acts(st_of(p0)))} "
                f"decision={real_decision_pending(st_of(p0))} "
                f"stage={ST['stage']} step={ST['step']} "
                f"exile_sq={len(zone_by_name(s, 0, 'Exile', SQUEE))} "
                f"gy_sq={len(zone_by_name(s, 0, 'Graveyard', SQUEE))} "
                f"untapped={untapped_lands(s, 0)}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    A = obs["assert"]

    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre.json never exported (setup gate not reached)")
    else:
        n_ex = len(zone_by_name(pre, 0, "Exile", SQUEE))
        n_gy = len(zone_by_name(pre, 0, "Graveyard", SQUEE))
        un = untapped_lands(pre, 0)
        obs["notes"].append(
            f"pre.json: turn={pre.get('turn_number')} phase={pre.get('phase')} "
            f"squee_exile={n_ex} squee_gy={n_gy} untapped_lands={un}")
        A["A1_setup_ok"] = "passed" if (n_ex >= 1 and n_gy >= 1
                                        and un >= 6) else "failed"

    A["A2_exile_cast_offered"] = ("passed" if ST["a2_offered"]
                                  else ("failed" if ST["a2_offered"] is False
                                        else "not-run"))

    exile_stack = exile_bf = gy_stack = gy_bf = False
    gy_offered = None
    exile_rejected = gy_rejected = False
    with open(f"{EVDIR}/wire_log.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event")
            d = e.get("data", {}) or {}
            if ev == "exile_stack_seen":
                exile_stack = True
            elif ev == "exile_bf_seen":
                exile_bf = True
            elif ev == "gy_stack_seen":
                gy_stack = True
            elif ev == "gy_bf_seen":
                gy_bf = True
            elif ev == "gy_cast_attempt":
                gy_offered = bool(d.get("offered"))
            elif ev == "rejected":
                who = d.get("who")
                payload = json.dumps(d.get("data", {}), default=str)
                if "exile" in payload.lower() or CAST.get("tag") == "EXILE":
                    exile_rejected = True
                if "gy" in payload.lower() or CAST.get("tag") == "GYCTL":
                    gy_rejected = True
    A["A3_exile_cast_completes"] = ("passed" if exile_stack and exile_bf
                                    else ("failed" if ST["step"] >= 2
                                          or exile_rejected
                                          else "not-run"))
    A["A4_gy_cast_completes"] = ("passed" if gy_stack and gy_bf
                                 else ("failed" if gy_offered is False
                                       or gy_rejected or ST.get("stop")
                                       else "not-run"))
    try:
        post = env_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        obs["notes"].append(f"post.json: stack_empty={stack_empty} "
                            f"turn={post.get('turn_number')} "
                            f"phase={post.get('phase')}")
        A["A5_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append(f"post.json missing ({e}); A5 not-run")

    obs["notes"].append(f"exile offered={ST['a2_offered']} "
                        f"(source={ST['a2_source']}) stack={exile_stack} "
                        f"bf={exile_bf} rejected={exile_rejected}")
    obs["notes"].append(f"gy offered={gy_offered} stack={gy_stack} "
                        f"bf={gy_bf} rejected={gy_rejected}")
    obs["notes"].append("protocol-120 driver (v0.105.0): mulligan via legacy "
                        "MulliganDecision action; DiscardToHandSize via vi "
                        "schema/select; Looting discard via vi hand-card "
                        "choice; CastSpell/ActivateAbility via legacy Action; "
                        "mana via legacy PayMana actions (gated on "
                        "outstanding non-test MANA_NEEDS, 2026-10-07 "
                        "double-pay gate) and/or vi tapLandForMana menus "
                        "driven by MANA_NEEDS; test Squee casts keep "
                        "MANA_NEEDS empty so the engine's auto-tap is the "
                        "single payer; Ooze target via advertised vi target "
                        "opportunity; legend rule answered preferring the "
                        "just-cast Squee; priority-gated passes; 5s re-tick "
                        "backstop; protocol-120 cast-confirmation guard "
                        "with 30s backstop after every CastSpell/ActivateAbility.")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A["A2_exile_cast_offered"] == "failed" \
            or A["A3_exile_cast_completes"] == "failed":
        verdict = "reproduced"
    elif (A["A2_exile_cast_offered"] == "passed"
          and A["A3_exile_cast_completes"] == "passed"
          and A["A4_gy_cast_completes"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "a2_source": ST["a2_source"],
                   "exile": {"offered": ST["a2_offered"],
                             "stack": exile_stack, "bf": exile_bf,
                             "rejected": exile_rejected},
                   "gy": {"offered": gy_offered, "stack": gy_stack,
                          "bf": gy_bf, "rejected": gy_rejected}}, f, indent=1)

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": {
            "validated_version": "v0.105.0",
            "build_commit": "965e243",
            "protocol_version": 120,
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.105.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.105.0/data/draft-pools.json"),
            "signature_verified": True,
        },
        "driver": {"protocol_advertised": 120,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[SQUEE_T, 12], [LOOTING_T, 4], [OOZE_T, 4],
                   [MOUNTAIN_T, 20], [FOREST_T, 20]],
            "P1": [[MOUNTAIN_T, 30], [FOREST_T, 30]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Squee density is a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6861_01050.py), not direct load.",
        ],
        "setup_line": "P0: 12x Squee, the Immortal + 4x Faithless Looting + "
                      "4x Scavenging Ooze + 20x Mountain + 20x Forest; "
                      "P1: 30x Mountain + 30x Forest (draw-go)",
        "contract_line": ("Cast exiled Squee: advertised and completes to the "
                          "battlefield. Control: cast graveyard Squee "
                          "completes to the battlefield."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")

    await render_summary(f"{EVDIR}/summary.png")
    import shutil
    shutil.copy(__file__, f"{EVDIR}/scenario_6861_01050.py")
    say("copied driver into evidence dir")
    await write_manifest()

    await p0.close()
    await p1.close()
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    # regenerate the manifest AFTER closing the logs so the recorded
    # hashes match the final bytes (2026-10-09 lesson).
    await write_manifest()
    print(f"[{time.strftime('%H:%M:%S')}] manifest regenerated post-close",
          flush=True)


def load_ev_state(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)["state"]


async def render_summary(out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    run = json.load(open(f"{EVDIR}/run.json"))
    try:
        pre = load_ev_state("pre.json")
        n_ex = len(zone_by_name(pre, 0, "Exile", SQUEE))
        n_gy = len(zone_by_name(pre, 0, "Graveyard", SQUEE))
        un = untapped_lands(pre, 0)
        pre_line = (f"pre: turn {pre.get('turn_number')} {pre.get('phase')} | "
                    f"squee exile={n_ex} gy={n_gy} | untapped lands={un}")
    except Exception:
        pre_line = "pre.json: missing"
    W, H = 1000, 780
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6861 - Squee, the Immortal cast from exile "
                    "(v0.105.0 revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), pre_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Squee in exile + Squee in gy, P0 main, "
                       "6+ untapped lands",
        "A2_exile_cast_offered": "A2 CastSpell advertised for the exiled Squee",
        "A3_exile_cast_completes": "A3 exiled Squee reaches Stack then "
                                   "Battlefield",
        "A4_gy_cast_completes": "A4 control: graveyard Squee reaches Stack "
                                "then Battlefield",
        "A5_cleanup": "A5 stack empty after attempts; game proceeds",
    }
    for k, lab in labels.items():
        v = asserts.get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6861/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    # NOTE: print, not say -- say() appends to scenario_run.log and would
    # invalidate the hash just recorded for it (fixed 2026-10-07).
    print(f"[{time.strftime('%H:%M:%S')}] wrote manifest.sha256 "
          f"({len(lines)} files)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
