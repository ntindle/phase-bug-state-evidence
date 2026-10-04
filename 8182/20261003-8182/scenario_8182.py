#!/usr/bin/env python3
"""Backfill scenario for phase-rs/phase #8182.

Reported: Timber Paladin enters as a 10/10 with vigilance and trample with
zero Auras attached (regression of #2418, fixed on main by #8186 / 9dd0d4e0
per Jacob Woodson's 2026-10-03 comment; systemic fail-open tracked in #8183).

Root cause per the issue: the three tiered aura-count conditions parsed to
StaticCondition::Unrecognized, which layers.rs evaluates as always-true, so
every tier applied and the 10/10 tier won on timestamp.

Pinned v0.101.0 card-data.json parses all three tiers as typed
QuantityComparison conditions (EQ 1 / EQ 2 / GE 3 on attached-Aura count) --
see data_evidence.json. This scenario asserts the RUNTIME OUTCOME on the
native engine, two human driver seats, protocol 103:

  A1: setup ok -- Timber Paladin on P0's battlefield.
  A2: REPORTED PATH -- with ZERO Auras attached: P/T must be the printed
      1/1, with no vigilance and no trample. 10/10 + vigilance + trample here
      is the reported defect.
  A3: tier control -- exactly 1 Aura attached: base 3/3 applies.
  A4: tier control -- exactly 2 Auras attached: base 5/5 + vigilance applies.
  A5: tier control -- exactly 3 Auras attached: base 10/10 + vigilance +
      trample applies.
  A6: data-level -- pinned parse carries typed aura-count conditions
      (not Unrecognized).

Auras are real Unholy Strength casts ({B}, "Enchant creature / Enchanted
creature gets +2/+1") targeting Timber Paladin, so expected final P/T is
base + 2n/+n for n auras: 5/4, 9/7, 16/13.

Verdict: reproduced iff A2 shows the reported 10/10 shape with zero Auras.
not-reproduced iff A2..A5 all pass on v0.101.0 (not a fix claim). Anything
less than a complete drive is blocked.
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
RUN_ID = "20261003-8182"
ISSUE = 8182
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.101.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned v0.101.0 server on 127.0.0.1:9374 "
                      "(started by an earlier backfill run; this run's own "
                      "game + raw Hello handshake verify the identity)"),
    "mode": "Full",
    "source": ("2026-10-03: latest stable release v0.101.0 (published "
               "2026-10-03T16:02:26Z) == pinned release dir; ServerHello "
               "0.101.0/acafe9b/protocol 103 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))

PALADIN = "timber paladin"
UNHOLY = "unholy strength"
FOREST = "forest"
SWAMP = "swamp"

P0_DECK = deck((PALADIN, 12), (UNHOLY, 12), (FOREST, 18), (SWAMP, 18))
P1_DECK = deck(("Island", 60))

BOTTOM_RANK = {PALADIN: 9, UNHOLY: 8, SWAMP: 2, FOREST: 1}
DISCARD_RANK = {PALADIN: 9, UNHOLY: 8, SWAMP: 2, FOREST: 1}

SETUP_DEADLINE_S = 600
DRIVE_DEADLINE_S = 1500

ASS_KEYS = ("A1_setup_ok", "A2_zero_aura_correct", "A3_one_aura",
            "A4_two_auras", "A5_three_auras", "A6_parse_level")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, LAND_PLAYED_TURN
    ST = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",   # setup -> zero -> aura1 -> aura2 -> aura3 -> done
        "pre_exported": False,
        "pre_turn": None,
        "post_0aura_exported": False,
        "post_aura_exported": set(),
        "aura_casting": False,
        "aura_count_seen": 0,
        "pal_oid": None,
        "paladin_seen_on_bf": False,
        "wf_types_seen": [],
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "defect_detail": {},
        "hello_ok": False,
        "tick": 0,
        "mana_needs": {"P0": {}, "P1": {}},
        "rejections": [],
    }
    MULLS = set()
    SUBMITTED_OPPS = set()
    LAND_PLAYED_TURN = {}


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


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def wf_of(state):
    return state.get("waiting_for") or {}


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


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
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and int(o.get("controller", -1)) == int(pid):
            out.append(int(oid))
    return out


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def untapped(o):
    return not o.get("tapped", False)


def untapped_lands(state, pid, lname=None):
    out = []
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and untapped(ob):
            if lname is None or obj_lname(state, o) == lname:
                out.append(o)
    return out


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def pt_of(state, oid):
    o = get_obj(state, oid)

    def num(v):
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return num(o.get("power")), num(o.get("toughness"))


def keywords_of(state, oid):
    o = get_obj(state, oid)
    out = set()
    for k in o.get("keywords") or []:
        if isinstance(k, dict):
            out.add(str(k.get("name") or k.get("keyword") or "").lower())
        else:
            out.add(str(k).lower())
    return out


def attached_aura_oids(state, pal_oid):
    """Oids of battlefield objects attached to the paladin (either link
    direction; deduped). Per attach-assertion hygiene both directions count."""
    found = set()
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        refs = set()
        at = o.get("attached_to")
        if at is not None:
            try:
                refs.add(int(at))
            except (TypeError, ValueError):
                pass
        for a in o.get("attachments") or []:
            try:
                refs.add(int(a))
            except (TypeError, ValueError):
                pass
        if int(pal_oid) in refs:
            found.add(int(oid))
    return found


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


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
    assert str(ver).startswith("0.101.0"), f"unexpected version {ver}"
    assert int(proto) == 103, f"unexpected protocol {proto}"
    assert str(build) == "acafe9b", f"unexpected build {build}"
    ST["hello_ok"] = True


# ------------------------------------------------------------- data check
def check_data_level():
    """Ground the premise: the pinned card-data's Timber Paladin parse must
    carry typed aura-count conditions (the #8186 fix), not Unrecognized."""
    tp = CARD_DATA.get("timber paladin", {})
    sas = tp.get("static_abilities") or []
    detail = {"n_static_abilities": len(sas), "tiers": []}
    want = [("EQ", 1, 3, 3, []), ("EQ", 2, 5, 5, ["Vigilance"]),
            ("GE", 3, 10, 10, ["Vigilance", "Trample"])]
    ok = len(sas) == 3
    for sa, (cmp_, cnt, pw, tw, kws) in zip(sas, want):
        cond = sa.get("condition") or {}
        mods = sa.get("modifications") or []
        kw_mods = sorted(m.get("keyword") for m in mods
                         if m.get("type") == "AddKeyword")
        pw_mods = [m.get("value") for m in mods if m.get("type") == "SetPower"]
        tw_mods = [m.get("value") for m in mods if m.get("type") == "SetToughness"]
        tier_ok = (cond.get("type") == "QuantityComparison"
                   and (cond.get("comparator")) == cmp_
                   and ((cond.get("rhs") or {}).get("value")) == cnt
                   and pw_mods == [pw] and tw_mods == [tw]
                   and kw_mods == sorted(kws))
        detail["tiers"].append({
            "description": sa.get("description"),
            "condition_type": cond.get("type"),
            "comparator": cond.get("comparator"),
            "rhs": (cond.get("rhs") or {}).get("value"),
            "set_pt": (pw_mods, tw_mods), "keywords": kw_mods,
            "tier_ok": tier_ok})
        ok = ok and tier_ok
    detail["oracle_text"] = tp.get("oracle_text")
    detail["printed_pt"] = (tp.get("power"), tp.get("toughness"))
    say(f"data-level check: {len(sas)} static abilities, tiers_ok={ok}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"detail": detail, "ok": ok,
                   "card_data_sha256": SERVER_IDENTITY["card_data_sha256"]}, f, indent=1)
    ST["data_level_ok"] = ok
    ST["defect_detail"] = detail
    wire("data_level", {"ok": ok, "detail": detail})


# ------------------------------------------------------------- actions
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


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    key = (tag, "mull", str(pend.get("mulligan_count", "?")))
    if key in MULLS:
        return False
    hn = hand_lnames(state, pid)
    MULLS.add(key)
    if tag == "P0" and PALADIN not in hn and len(hn) > 5:
        say(f"[{tag}] mulligan (no Paladin, hand={len(hn)})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
        return True
    say(f"[{tag}] keep {len(hn)} (hand: {hn})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")))
    if key in SUBMITTED_OPPS:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    picks = sorted(hand, key=lambda o: (BOTTOM_RANK.get(obj_lname(state, o), 5), o))[:n]
    picks = [int(o) for o in picks]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False
    key = (tag, "discard", str(st.get("state_revision")))
    if key in SUBMITTED_OPPS:
        return False
    hand = hand_ids(state, pid)
    picks = sorted(hand, key=lambda o: (DISCARD_RANK.get(obj_lname(state, o), 5), o))[:n]
    picks = [int(o) for o in picks]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("discard", {"who": tag, "picks": picks})
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
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
        if sum(needs.values()) <= 0:
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
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"iid": iid, "used_for": used})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick["id"]}}}, tag)
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
                                                   "data": {"choiceId": ch.get("id")}}}, c.name)
                return True
    return False


def _cand_kind(ch):
    for sf in ch.get("surfaces", []) or []:
        t = sf.get("type")
        d = sf.get("data") or {}
        if t == "object":
            try:
                return ("object", int(d.get("reference")))
            except (TypeError, ValueError):
                continue
        if t == "player":
            for k in ("reference", "player", "player_id", "id", "seat"):
                try:
                    v = d.get(k)
                    if v is None:
                        continue
                    return ("player", int(v))
                except (TypeError, ValueError):
                    continue
    return ("other", None)


def _choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


async def answer_paladin_target(c, st, state, tag):
    """Unholy Strength's TargetSelection: pick the Paladin. Object-candidate
    schema; matches on serialized content (paladin oid), never a bare
    numeric needle. Quiet-window fingerprint per (iid, pick)."""
    if not ST.get("aura_casting"):
        return False
    pal = ST.get("pal_oid")
    if pal is None:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or data.get("choices") or []
        kinds = [_cand_kind(ch) for ch in chs]
        pick_ch = None
        for ch, (k, ref) in zip(chs, kinds):
            if k == "object" and int(ref) == int(pal):
                pick_ch = ch
                break
        if pick_ch is None:
            for ch in chs:
                if _choice_text(ch).lower() == "timber paladin":
                    pick_ch = ch
                    break
        if pick_ch is None:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        key = (str(iid), str(pick_ch.get("id")))
        last = ST.get("_tgt_last")
        if last and last["key"] == key and ST["tick"] - last["tick"] <= 3:
            continue
        ST["_tgt_last"] = {"key": key, "tick": ST["tick"]}
        say(f"[{tag}] targeting Timber Paladin (oid {pal}) with Unholy Strength "
            f"(iid={iid}) -- SUBMITTED")
        wire("aura_target", {"who": tag, "iid": iid, "pal_oid": pal})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec.get("type"),
                                           "data": {"choiceIds": [pick_ch.get("id")]}}}, tag)
        return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


# ------------------------------------------------------------- P0 tick
async def p0_tick(c):
    st = c.latest
    if not st:
        return
    ST["tick"] += 1
    state = st["state"]
    pid = c.player_id
    wtype = wf_of(state).get("type") or ""
    if wtype not in ST["wf_types_seen"]:
        ST["wf_types_seen"].append(wtype)
        say(f"[P0] waiting_for type: {wtype}")
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0"):
            return
        if await do_bottom(c, pid, "P0"):
            return
        return
    if wtype in ("BottomCards",):
        if await do_bottom(c, pid, "P0"):
            return
        return
    if await do_discard(c, pid, "P0"):
        return
    acts = merged_actions(st)
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, "P0"):
        return
    if await answer_paladin_target(c, st, state, "P0"):
        return

    pal_oids = bf_by_name(state, 0, PALADIN)
    if pal_oids and ST["pal_oid"] is None:
        ST["pal_oid"] = pal_oids[0]
        ST["paladin_seen_on_bf"] = True
        say(f"[P0] Timber Paladin on battlefield (oid {ST['pal_oid']})")
        wire("paladin_on_bf", {"oid": ST["pal_oid"]})

    n_auras = len(attached_aura_oids(state, ST["pal_oid"])) if ST["pal_oid"] else 0
    if ST["pal_oid"] and n_auras != ST["aura_count_seen"]:
        say(f"[P0] attached aura count: {ST['aura_count_seen']} -> {n_auras}")
        wire("aura_count", {"n": n_auras})
        ST["aura_count_seen"] = n_auras

    # pre.json: first quiescent observation of the Paladin on the
    # battlefield (empty stack), before any aura is attached.
    if (ST["paladin_seen_on_bf"] and not ST["pre_exported"]
            and not stack_entries(state) and n_auras == 0):
        await export_as(c, "pre")
        ST["pre_exported"] = True
        ST["pre_turn"] = state.get("turn_number")
        pre_st = json.load(open(f"{EVDIR}/pre.json")).get("state", {})
        pt = pt_of(pre_st, ST["pal_oid"])
        kws = sorted(keywords_of(pre_st, ST["pal_oid"]))
        say(f"[P0] pre.json exported at turn {ST['pre_turn']}: "
            f"paladin pt={pt} keywords={kws} auras=0")
        wire("pre_exported", {"pt": pt, "keywords": kws})
        ST["phase"] = "zero"
        return

    # post_0aura.json: a later quiescent state, still zero auras -- proves
    # the zero-aura stat line persists (the reported defect is continuous).
    if (ST["phase"] == "zero" and not ST["post_0aura_exported"]
            and not stack_entries(state) and n_auras == 0
            and state.get("turn_number") != ST["pre_turn"]):
        await export_as(c, "post_0aura")
        ST["post_0aura_exported"] = True
        say(f"[P0] post_0aura.json exported at turn {state.get('turn_number')}")
        wire("post_0aura_exported", {})
        ST["phase"] = "aura1"
        return

    # aura legs: after each Unholy Strength resolves and attaches, export
    # post_{n}aura.json once, then advance.
    if ST["phase"] in ("aura1", "aura2", "aura3"):
        want_n = {"aura1": 1, "aura2": 2, "aura3": 3}[ST["phase"]]
        if n_auras >= want_n and not stack_entries(state) \
                and want_n not in ST["post_aura_exported"]:
            await export_as(c, f"post_{want_n}aura")
            ST["post_aura_exported"].add(want_n)
            post_st = json.load(open(f"{EVDIR}/post_{want_n}aura.json")).get("state", {})
            pt = pt_of(post_st, ST["pal_oid"])
            kws = sorted(keywords_of(post_st, ST["pal_oid"]))
            say(f"[P0] post_{want_n}aura.json exported: paladin pt={pt} "
                f"keywords={kws} auras={n_auras}")
            wire("post_aura_exported", {"n": want_n, "pt": pt, "keywords": kws})
            ST["aura_casting"] = False
            ST["phase"] = {1: "aura2", 2: "aura3", 3: "done"}[want_n]
            if ST["phase"] == "done":
                return

    # build actions on own main phase with empty stack
    if my_main(state, pid):
        hn = hand_lnames(state, pid)
        # cast Timber Paladin ({1}{G})
        if (ST["phase"] == "setup" and not ST["paladin_seen_on_bf"]
                and PALADIN in hn and len(untapped_lands(state, pid)) >= 2
                and untapped_lands(state, pid, FOREST)):
            a, oid = cast_action_for(acts, state, PALADIN)
            if a:
                ST["mana_needs"]["P0"] = {"G": 1, "generic": 1}
                say(f"[P0] casting Timber Paladin (oid {oid}) -- DECISIVE")
                wire("paladin_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        # cast Unholy Strength on the Paladin (one at a time)
        if (ST["phase"] in ("aura1", "aura2", "aura3")
                and ST["paladin_seen_on_bf"] and not ST["aura_casting"]
                and UNHOLY in hn and untapped_lands(state, pid, SWAMP)):
            want_n = {"aura1": 1, "aura2": 2, "aura3": 3}[ST["phase"]]
            if n_auras < want_n:
                a, oid = cast_action_for(acts, state, UNHOLY)
                if a:
                    ST["mana_needs"]["P0"] = {"B": 1}
                    ST["aura_casting"] = True
                    say(f"[P0] casting Unholy Strength (oid {oid}) targeting "
                        f"Paladin for aura #{want_n} -- DECISIVE")
                    wire("unholy_cast", {"oid": oid, "aura_n": want_n})
                    await submit_as_is(c, a)
                    return
        # play a land
        turn = state.get("turn_number")
        if LAND_PLAYED_TURN.get("P0") != turn:
            for a in acts:
                if a["type"] == "PlayLand":
                    LAND_PLAYED_TURN["P0"] = turn
                    say(f"[P0] playing land (turn {turn})")
                    await submit_as_is(c, a)
                    return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid = c.player_id
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1"):
            return
        if await do_bottom(c, pid, "P1"):
            return
        return
    if wtype in ("BottomCards",):
        if await do_bottom(c, pid, "P1"):
            return
        return
    if await do_discard(c, pid, "P1"):
        return
    acts = merged_actions(st)
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if my_main(state, pid):
        turn = state.get("turn_number")
        if LAND_PLAYED_TURN.get("P1") != turn:
            for a in acts:
                if a["type"] == "PlayLand":
                    LAND_PLAYED_TURN["P1"] = turn
                    await submit_as_is(c, a)
                    return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)


async def main():
    reset_attempt()
    check_data_level()
    await verify_server_hello()

    c0 = PhaseClient("P0")
    c1 = PhaseClient("P1")
    await c0.connect()
    await c1.connect()
    say("P0 hello done; P1 hello done")

    attached = await c0.create(P0_DECK, player_count=2)
    code = attached.get("game_code")
    ST["game_code"] = code
    say(f"game created: code={code}")
    wire("game_created", {"code": code})
    j1 = await c1.join(code, P1_DECK)
    say(f"P1 joined: {json.dumps(j1)[:160]}")
    wire("game_joined", {"j1": j1})

    t0 = time.time()
    try:
        # ---- setup: leave mulligan phases
        while time.time() - t0 < SETUP_DEADLINE_S:
            await asyncio.sleep(1.0)
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            st = c0.latest
            if not st:
                continue
            wt = wf_of(st["state"]).get("type") or ""
            if wt not in ("MulliganDecision", "BottomCards"):
                ST["post_mulligan_seen"] = True
                break
        else:
            say("SETUP DEADLINE hit without leaving mulligan phases")
            wire("deadline", {"which": "setup"})

        if not ST["post_mulligan_seen"]:
            say("never left mulligan phases; exporting post.json for the record")
            await export_as(c0, "post")
            return

        # ---- drive: paladin cast, zero-aura observation, three aura legs
        say("setup done; driving paladin + aura legs")
        t1 = time.time()
        while time.time() - t1 < DRIVE_DEADLINE_S and ST["phase"] != "done":
            await asyncio.sleep(0.5)
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
        say(f"drive ended: phase={ST['phase']}")
    finally:
        await finalize(c0)
        await c0.close()
        await c1.close()
        if not WIRE.closed:
            WIRE.close()
        if not RUNLOG.closed:
            RUNLOG.close()


# ------------------------------------------------------------- finalize
def load_env(name):
    try:
        return json.load(open(f"{EVDIR}/{name}.json"))
    except Exception as e:
        ST["notes"].append(f"{name}.json load failed: {e}")
        return None


def paladin_facts(state, pal_oid):
    """(power, toughness, keywords_set, n_attached_auras) from a saved state."""
    pt = pt_of(state, pal_oid)
    kws = keywords_of(state, pal_oid)
    n = len(attached_aura_oids(state, pal_oid))
    return pt[0], pt[1], kws, n


async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]

    # A6: data-level parse carries typed aura-count conditions
    detail = ST.get("defect_detail") or {}
    if ST.get("data_level_ok"):
        ass["A6_parse_level"] = "passed"
        notes.append("A6 passed: pinned v0.101.0 card-data.json parses all "
                     "three Timber Paladin tiers as typed QuantityComparison "
                     "aura-count conditions (EQ 1 / EQ 2 / GE 3) -- not "
                     "Unrecognized. See data_evidence.json.")
    else:
        ass["A6_parse_level"] = "failed"
        notes.append("A6 FAILED: the pinned parse does not carry typed "
                     "aura-count conditions; see data_evidence.json: "
                     f"{json.dumps(detail)[:400]}")

    # A1: setup
    pre = load_env("pre") or {}
    pre_st = pre.get("state") or {}
    pal = ST.get("pal_oid")
    if ST["paladin_seen_on_bf"] and pal and pre_st:
        ass["A1_setup_ok"] = "passed"
        notes.append(f"A1 passed: game {ST['game_code']} started for both "
                     f"seats; Timber Paladin (oid {pal}) on P0's battlefield; "
                     f"pre.json exported at turn {ST['pre_turn']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: Timber Paladin never reached P0's "
                     "battlefield (setup not established).")

    # locate the paladin oid inside each exported state (oids may differ
    # only if re-exported; they are stable within a game)
    def find_pal(st8):
        for oid, o in (st8.get("objects") or {}).items():
            if o.get("zone") == "Battlefield" and str(
                    o.get("base_name") or o.get("name") or "").lower() == PALADIN:
                return int(oid)
        return None

    # A2: the reported path -- zero Auras attached
    zero = load_env("post_0aura") or pre
    zero_st = zero.get("state") or {}
    pal_z = find_pal(zero_st) or pal
    if ass["A1_setup_ok"] == "passed" and pal_z and zero_st:
        p, t, kws, n = paladin_facts(zero_st, pal_z)
        notes.append(f"A2 probe: zero-aura state has paladin oid {pal_z} "
                     f"pt={p}/{t} keywords={sorted(kws)} attached_auras={n} "
                     f"(source: {'post_0aura.json' if load_env('post_0aura') else 'pre.json'})")
        if n != 0:
            ass["A2_zero_aura_correct"] = "not-run"
            notes.append("A2 not-run: the zero-aura state has auras attached; "
                         "the reported path was not isolated.")
        elif (p, t) == (1, 1) and "vigilance" not in kws and "trample" not in kws:
            ass["A2_zero_aura_correct"] = "passed"
            notes.append("A2 passed: with zero Auras attached Timber Paladin "
                         "is 1/1 with no vigilance and no trample -- the "
                         "reported 10/10 shape does not occur.")
        elif (p, t) == (10, 10) and "vigilance" in kws and "trample" in kws:
            ass["A2_zero_aura_correct"] = "failed"
            notes.append("A2 FAILED: with ZERO Auras attached Timber Paladin "
                         "is 10/10 with vigilance and trample -- the exact "
                         "reported defect of #8182 reproduces on v0.101.0.")
        else:
            ass["A2_zero_aura_correct"] = "failed"
            notes.append(f"A2 FAILED: unexpected zero-aura stat line {p}/{t} "
                         f"keywords={sorted(kws)} -- neither the correct 1/1 "
                         f"nor the reported 10/10 shape.")
    else:
        ass["A2_zero_aura_correct"] = "not-run"
        notes.append("A2 not-run: A1 failed.")

    # A3-A5: tier controls from the per-aura exports
    tier_expect = {
        "A3_one_aura": (1, 3, 3, set()),
        "A4_two_auras": (2, 5, 5, {"vigilance"}),
        "A5_three_auras": (3, 10, 10, {"vigilance", "trample"}),
    }
    for akey, (n, base_p, base_t, want_kws) in tier_expect.items():
        env = load_env(f"post_{n}aura")
        st8 = (env or {}).get("state") or {}
        pal_n = find_pal(st8)
        if pal_n is None or not st8:
            ass[akey] = "not-run"
            notes.append(f"{akey} not-run: post_{n}aura.json missing or no "
                         f"paladin on the battlefield.")
            continue
        p, t, kws, na = paladin_facts(st8, pal_n)
        exp_p, exp_t = base_p + 2 * n, base_t + n
        notes.append(f"{akey} probe: post_{n}aura.json paladin pt={p}/{t} "
                     f"(expected base {base_p}/{base_t} + {n}x Unholy "
                     f"Strength (+2/+1) = {exp_p}/{exp_t}) "
                     f"keywords={sorted(kws)} (expected {sorted(want_kws)}) "
                     f"attached_auras={na}")
        if na != n:
            ass[akey] = "not-run"
            notes.append(f"{akey} not-run: attached aura count is {na}, "
                         f"not {n}.")
        elif (p, t) == (exp_p, exp_t) and want_kws <= kws \
                and ({"vigilance", "trample"} - want_kws).isdisjoint(kws):
            ass[akey] = "passed"
            notes.append(f"{akey} passed: tier {n} applies correctly "
                         f"({exp_p}/{exp_t}"
                         f"{' + ' + '/'.join(sorted(want_kws)) if want_kws else ''}).")
        else:
            ass[akey] = "failed"
            notes.append(f"{akey} FAILED: tier-{n} stat line wrong.")

    # verdict
    a1, a2 = ass["A1_setup_ok"], ass["A2_zero_aura_correct"]
    a3, a4, a5 = ass["A3_one_aura"], ass["A4_two_auras"], ass["A5_three_auras"]
    if a1 != "passed":
        verdict = "blocked"
    elif a2 == "failed":
        verdict = "reproduced"
    elif all(x == "passed" for x in (a2, a3, a4, a5)):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"verdict: {verdict} (assertions: {ass})")

    if verdict == "reproduced":
        result = ("Timber Paladin with ZERO Auras attached is 10/10 with "
                  "vigilance and trample on v0.101.0 -- the exact reported "
                  "defect of #8182 reproduces (regression of #2418).")
    elif verdict == "not-reproduced":
        result = ("Timber Paladin with zero Auras attached is 1/1 with no "
                  "vigilance and no trample on v0.101.0, and the 1/2/3-Aura "
                  "tiers apply correctly (5/4; 9/7+vigilance; 16/13+vigilance"
                  "+trample, each including the attached Unholy Strengths' "
                  "+2/+1). The reported 10/10 shape does not occur; the "
                  "pinned parse carries typed aura-count conditions "
                  "(EQ 1 / EQ 2 / GE 3). Scoped to v0.101.0 -- not a fix claim.")
    else:
        result = ("Blocked: " + "; ".join(
            n for n in notes if "FAILED" in n or "not-run" in n))

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "scope": ("Timber Paladin tiered aura-count statics: zero-aura "
                  "stat line (the reported path) plus 1/2/3-Aura tier "
                  "controls with real Unholy Strength casts; native engine, "
                  "two human driver seats, protocol-103 driver. Asserts the "
                  "runtime OUTCOME (P/T + keywords) against the pinned "
                  "QuantityComparison parse, with aura attachment grounded "
                  "in the attached_to/attachments link (both directions)."),
        "result": result,
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Timber Paladin / 12x Unholy Strength deck density is a test-harness convenience (engine accepts >4-of for custom games).",
            "Tier stat lines include the attached Unholy Strengths' continuous +2/+1 each; the base-set assertions are arithmetic on top of that.",
            "Not tested on the report's original 2026-08-29 build; verdict is scoped to v0.101.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports restorable only via full game replay.",
            "P1 is a passive land-drop seat; no combat was driven.",
            "The secondary fail-open finding (StaticCondition::Unrecognized evaluates true in layers.rs) is tracked separately in #8183 and was not exercised here -- the pinned parse has no Unrecognized conditions for this card.",
        ],
        "observations": {
            "paladin_oid": pal,
            "pre_turn": ST["pre_turn"],
            "post_aura_exports": sorted(ST["post_aura_exported"]),
            "wf_types_seen": ST["wf_types_seen"],
            "game_code": ST["game_code"],
            "drive_phase": ST["phase"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_8182.py", "w") as f:
        f.write(open(__file__).read())
    say(f"run.json written; verdict={verdict}")


if __name__ == "__main__":
    asyncio.run(main())
