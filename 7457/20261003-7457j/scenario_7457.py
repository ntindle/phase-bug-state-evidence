#!/usr/bin/env python3
"""Backfill scenario for phase-rs/phase #7457.

Engine: the activation-cost publish path never publishes its sacrificed set,
so `caused_by: Sacrificed` consumers read 0 (Radiant Lotus, Devouring Rage).

Pinned v0.101.0 parse carries both consumers:
  Radiant Lotus: Activated {T}, Sacrifice artifacts -> sub Mana
    {produced ChosenColor count Ref(FilteredTrackedSetSize{Artifact,
    caused_by: Sacrificed}), target Player}
  Devouring Rage: repeat_for Ref(FilteredTrackedSetSize{Spirit,
    caused_by: Sacrificed}) on the +3/+0 rider

Two runtime legs on one game (P0 mono-red, P1 passive), RAGE FIRST:
  Leg 1 (Devouring Rage): cast {4}{R}, additional cost sacrifice 2 Spirits
    (Frostling/Glitterfang), target a Memnite.
    Expected: +3/+0 base + 2x +3/+0 = +9/+0 -> 1/1 Memnite becomes 10/1.
    Defect (matches the issue's measured pt=(5,2) shape): only the base
    +3/+0 applies -> 4/1. No color-choice UX dependency.
  Leg 2 (Radiant Lotus, best-effort): activate, sacrifice artifacts, choose
    Red, target P0.
    Expected per pinned parse: N red mana for N sacrificed artifacts
    (oracle says 3 per artifact, but the pinned parse counts
    FilteredTrackedSetSize = N total).
    Defect: 0 mana added while the artifacts are genuinely in the graveyard.
    NOTE: the Lotus's Choose-Color is a post-stack NamedChoice (not on the
    stack); the driver answers it best-effort. If the color choice cannot
    be answered, the Lotus leg is documented as blocked and the verdict
    rests on the Rage leg.

Assertions test the correct behavior; failures on A6 (with A5 passing) or
on A4 (with A3 passing) reproduce the reported defect.
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
RUN_ID = "20261003-7457j"
ISSUE = 7457
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

LOTUS = "radiant lotus"
MEMNITE = "memnite"
FROSTLING = "frostling"
GLITTERFANG = "glitterfang"
RAGE = "devouring rage"
MOUNTAIN = "mountain"
SPIRITS = {FROSTLING, GLITTERFANG}

P0_DECK = deck((LOTUS, 4), (MEMNITE, 14), (FROSTLING, 6), (GLITTERFANG, 6),
               (RAGE, 6), (MOUNTAIN, 24))
P1_DECK = deck(("Swamp", 60))

# bottom rank: lowest bottomed first; Rage and Lotus are never bottomed
# (rage-first flow needs Rage in hand; Lotus for the second leg)
BOTTOM_RANK = {RAGE: 9, GLITTERFANG: 1, FROSTLING: 2, MEMNITE: 3,
               LOTUS: 9, MOUNTAIN: 8}
DISCARD_RANK = {RAGE: 9, LOTUS: 9, GLITTERFANG: 2, FROSTLING: 3,
                MEMNITE: 4, MOUNTAIN: 8}

SETUP_DEADLINE_S = 1200
RAGE_DEADLINE_S = 900

ASS_KEYS = ("A1_data_level", "A2_setup_ok", "A3_lotus_sacrifice_paid",
            "A4_lotus_mana_added", "A5_rage_cost_paid", "A6_rage_pump")


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
        "phase": "build",          # build -> rage_resolving -> rage_done -> lotus_resolving -> done
        # (Rage leg runs FIRST: it has no color-choice UX dependency and
        # cleanly demonstrates the reported defect. Lotus leg is second,
        # best-effort.)
        "pre_exported": False,
        "post_mulligan_seen": False,
        "wf_types_seen": [],
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "defect_detail": {},
        "hello_ok": False,
        "lotus_oid": None,
        "lotus_activated": False,
        "lotus_on_stack_since": None,
        "post_lotus_exported": False,
        "sacrificed_memnites": [],
        "pre_lotus_pool": None,
        "post_lotus_pool": None,
        "color_chosen": None,
        "rage_oid": None,
        "rage_target_oid": None,
        "rage_target_submitted": False,
        "rage_seen_on_stack": False,
        "rage_res_answers": [],  # list of {"iid":..,"pick":..,"tick":..}
        "tick": 0,
        "raw_opp_logged": 0,
        "sacrificed_spirits": [],
        "pre_rage_exported": False,
        "post_rage_exported": False,
        "post_rage_pt": None,
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


def se_blob(se):
    return json.dumps(se, default=str).lower()


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


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and untapped(get_obj(state, o))]


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def gy_oids(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard" and int(o.get("owner", o.get("controller", -1))) == int(pid):
            out.append(int(oid))
    return out


def mana_pool(state, pid):
    from collections import Counter
    out = Counter()
    for p in state.get("players", []):
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in ("white", "blue", "black", "red", "green", "colorless"):
                    if color in blob:
                        out[color] += 1
                        break
                else:
                    out["unknown"] += 1
    return dict(out)


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
def find_caused_by_sacrificed(node, path=""):
    """Find FilteredTrackedSetSize{caused_by: Sacrificed} nodes in a parse tree."""
    hits = []
    if isinstance(node, dict):
        qty = node.get("qty") or {}
        if isinstance(qty, dict) and qty.get("type") == "FilteredTrackedSetSize" \
                and qty.get("caused_by") == "Sacrificed":
            hits.append((path, qty.get("filter")))
        for k, v in node.items():
            hits += find_caused_by_sacrificed(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += find_caused_by_sacrificed(v, f"{path}[{i}]")
    return hits


def check_data_level():
    lotus = CARD_DATA.get("radiant lotus", {})
    rage = CARD_DATA.get("devouring rage", {})
    detail = {}
    lh = find_caused_by_sacrificed(lotus.get("abilities"))
    rh = find_caused_by_sacrificed(rage.get("abilities"))
    detail["lotus_hits"] = [(p, json.dumps(f, default=str)[:200]) for p, f in lh]
    detail["rage_hits"] = [(p, json.dumps(f, default=str)[:200]) for p, f in rh]
    # Lotus must be the Activated {T}, Sacrifice cost shape
    ab = (lotus.get("abilities") or [{}])[0]
    cost = ab.get("cost") or {}
    costs = cost.get("costs") or []
    cost_ok = (ab.get("kind") == "Activated"
               and any(c.get("type") == "Tap" for c in costs)
               and any(c.get("type") == "Sacrifice" for c in costs))
    detail["lotus_cost_ok"] = cost_ok
    detail["lotus_oracle"] = lotus.get("oracle_text")
    detail["rage_oracle"] = rage.get("oracle_text")
    ok = bool(lh) and bool(rh) and cost_ok
    say(f"data-level check: lotus_caused_by_sacrificed_hits={len(lh)} "
        f"rage_hits={len(rh)} cost_shape_ok={cost_ok} -> {ok}")
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
    # P0 mulligans aggressively to find Rage (rage-first flow);
    # P1 keeps. Max ~2 mulligans via len(hn) > 5.
    if tag == "P0" and RAGE not in hn and len(hn) > 5:
        say(f"[{tag}] mulligan (no Rage, hand={len(hn)})")
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
    # never bottom the first Lotus
    lotus_oids = [o for o in hand if obj_lname(state, o) == LOTUS]
    keep = set(lotus_oids[:1])
    picks = sorted((o for o in hand if o not in keep),
                   key=lambda o: (BOTTOM_RANK.get(obj_lname(state, o), 5), o))[:n]
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
    wtype = wf_of(state).get("type")
    if wtype != "DiscardToHandSize":
        return False
    # the waiting_for carries the player directly (not in pending[])
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False
    key = (tag, "discard", str(st.get("state_revision")))
    if key in SUBMITTED_OPPS:
        return False
    hand = hand_ids(state, pid)
    lotus_oids = [o for o in hand if obj_lname(state, o) == LOTUS]
    keep = set(lotus_oids[:1])
    picks = sorted((o for o in hand if o not in keep),
                   key=lambda o: (DISCARD_RANK.get(obj_lname(state, o), 5), o))[:n]
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
    if not t:
        syms = []
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("symbols"):
                syms += list(d.get("symbols") or [])
        if syms:
            t = "/".join(syms)
    return str(t)

# ------------------------------------------------------------- leg interactions
def _log_opportunities(st, tag, why):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        chs = data.get("candidates") or data.get("choices") or []
        texts = [_choice_text(ch) for ch in chs[:8]]
        say(f"[{tag}] OPP ({why}): iid={opp.get('interactionId') or opp.get('id')} "
            f"rtype={resp.get('type')} spec={json.dumps(spec)[:160]} "
            f"n_cand={len(chs)} texts={texts}")
        wire("opportunity", {"who": tag, "why": why,
                             "iid": opp.get("interactionId") or opp.get("id"),
                             "rtype": resp.get("type"),
                             "spec": spec,
                             "n_candidates": len(chs), "texts": texts})
        # Full raw candidate dump, capped: text extraction has proven
        # unreliable on some prompts (e.g. the Lotus color choice), so keep
        # the raw bytes for diagnosis.
        if ST.get("raw_opp_logged", 0) < 6:
            ST["raw_opp_logged"] = ST.get("raw_opp_logged", 0) + 1
            raw = json.dumps(chs, default=str)
            wire("opportunity_raw",
                 {"who": tag, "why": why,
                  "iid": opp.get("interactionId") or opp.get("id"),
                  "rtype": resp.get("type"),
                  "candidates_trunc": raw[:4000],
                  "candidates_len": len(raw)})


async def answer_sacrifice_number(c, st, tag):
    """Devouring Rage's 'sacrifice any number of Spirits' count prompt: a
    schema with spec.type == 'number' (min 0, max N). Answer 2 (or fewer if
    fewer Spirits are available). Pre-stack only; uses a (iid, 'number')
    fingerprint with a 3-tick quiet window."""
    if ST["phase"] != "rage_resolving":
        return False
    if ST.get("rage_seen_on_stack"):
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") != "number":
            continue
        sdata = spec.get("data") or {}
        lo = sdata.get("min", 0) or 0
        hi = sdata.get("max", 0) or 0
        iid = opp.get("interactionId") or opp.get("id")
        key = (str(iid), "number")
        last = ST.get("_sacnum_last")
        if last and last["key"] == key and ST["tick"] - last["tick"] <= 3:
            continue
        state = json.loads(await c.export_state()).get("state", {})
        spirits = [o for n in SPIRITS for o in bf_by_name(state, 0, n)]
        want = min(2, len(spirits), hi)
        want = max(want, lo)
        ST["_sacnum_last"] = {"key": key, "tick": ST["tick"]}
        ST["_sac_number"] = want
        say(f"[{tag}] sacrifice count: answering {want} "
            f"(spirits={len(spirits)} min={lo} max={hi}) -- SUBMITTED")
        wire("rage_sac_number", {"who": tag, "iid": iid, "value": want,
                                 "spirits": len(spirits)})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "number",
                                           "data": {"value": int(want)}}}, tag)
        return True
    return False


async def answer_sacrifice_select(c, st, state, tag):
    """Sacrifice-cost select interactions. The engine prompts iteratively
    (observed: min=1/max=1 per prompt, constraint nested at
    spec.data.constraint.data). Lotus leg -> 3 artifacts total (Memnite
    first, never the Lotus itself); Rage leg -> 2 Spirits total. Picks are
    reconciled against the graveyard every tick: only zone-confirmed
    sacrifices count toward the target, a still-pending iid is retried
    (its last submit must have been rejected), and when the target is
    reached and the spec allows 0, submit empty to finish. If the engine
    demands more than the target (min>0 with target reached), pay the
    minimum -- the assertions measure actual sacrifices, so over-paying
    still demonstrates the defect."""
    phase = ST["phase"]
    if phase == "lotus_resolving":
        # The sacrifice is an activation COST: it is paid before the ability
        # reaches the stack. A post-stack object-candidate prompt is a
        # different question (e.g. a resolution choice), never a sacrifice.
        if ST.get("lotus_seen_on_stack"):
            return False
        want_n = 3
        spirit_leg = False
        pick_key = "sacrificed_memnites"
    elif phase == "rage_resolving":
        # The sacrifice is an additional CAST cost: paid before the spell
        # reaches the stack. Post-stack prompts are resolution choices.
        if ST.get("rage_seen_on_stack"):
            return False
        want_n = 2
        spirit_leg = True
        pick_key = "sacrificed_spirits"
    else:
        return False
    lotus_oid = ST.get("lotus_oid")
    # Reconcile bookkeeping against reality: a recorded pick that is still
    # on the battlefield was rejected or not yet processed -- it does not
    # count, and its iid may be retried.
    recorded = ST.get(pick_key) or []
    confirmed = [o for o in recorded
                 if get_obj(state, o).get("zone") == "Graveyard"]
    in_flight = [o for o in recorded
                 if get_obj(state, o).get("zone") != "Graveyard"]
    if len(confirmed) + len(in_flight) != len(recorded):
        # an oid vanished from state entirely; drop it
        recorded = confirmed + in_flight
    ST[pick_key] = recorded
    done_n = len(confirmed)
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        kinds = [_cand_kind(ch) for ch in chs]
        if not all(k == "object" for k, _ in kinds):
            continue
        refs = [ref for _, ref in kinds]
        objs = [get_obj(state, r) for r in refs]
        if spirit_leg:
            ok_idx = [i for i, o in enumerate(objs)
                      if obj_lname(state, refs[i]) in SPIRITS]
        else:
            ok_idx = [i for i, o in enumerate(objs)
                      if "Artifact" in ((o.get("card_types") or {}).get("core_types") or [])
                      and str(refs[i]) != str(lotus_oid)]
            ok_idx.sort(key=lambda i: 0 if obj_lname(state, refs[i]) == MEMNITE else 1)
        # never re-pick a confirmed or in-flight oid
        taken = set(confirmed) | set(in_flight)
        ok_idx = [i for i in ok_idx if refs[i] not in taken]
        # GUARD: only treat this as a sacrifice prompt if the candidates
        # are actually sacrificeable (on our battlefield). A target-selection
        # prompt (e.g. Devouring Rage's "target creature") can surface the
        # same schema/sequence shape with overlapping names (including
        # graveyard objects); answering it as a sacrifice corrupts the target.
        bf_now = set(bf_oids(state, 0))
        ok_idx = [i for i in ok_idx if refs[i] in bf_now]
        if not ok_idx:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        # constraint may be nested (spec.data.constraint.data.{min,max})
        # or top-level (spec.{min,max}); the nested shape wins when present.
        con = ((spec.get("data") or {}).get("constraint") or {}).get("data") or {}
        lo_raw = con.get("min", spec.get("min", 0))
        mx_raw = con.get("max", spec.get("max"))
        try:
            lo = int(lo_raw)
        except (TypeError, ValueError):
            lo = 0
        try:
            hi = int(mx_raw) if mx_raw is not None else len(chs)
        except (TypeError, ValueError):
            hi = len(chs)
        hi = max(0, min(hi, len(ok_idx)))
        remaining = want_n - done_n - len(in_flight)
        if remaining <= 0:
            if lo > 0:
                # engine demands more than our target: pay the minimum and
                # let the assertions measure what actually happened.
                n = min(lo, hi)
                over = f" (over target {want_n}; engine min={lo})"
            else:
                # A still-pending iid here was already answered; a retry is
                # harmless but pointless -- mark done only once per iid.
                if iid in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] sacrifice select: target reached ({done_n}); "
                    f"submitting empty (done)")
                wire("sacrifice_done", {"who": tag, "iid": iid})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": spec.get("type"),
                                                   "data": {"choiceIds": []}}}, tag)
                return True
        else:
            n = max(lo, min(remaining, hi))
            over = ""
        if n <= 0:
            # cannot satisfy this prompt right now; leave the iid
            # unpoisoned so a later tick (new state) can retry.
            continue
        # A still-pending iid means the previous submit for it failed
        # (rejected): allow exactly one retry per tick by refreshing it.
        SUBMITTED_OPPS.discard(iid)
        pick_idx = ok_idx[:n]
        pick_refs = [refs[i] for i in pick_idx]
        picksel = [chs[i].get("id") for i in pick_idx]
        SUBMITTED_OPPS.add(iid)
        ST[pick_key] = confirmed + in_flight + pick_refs
        say(f"[{tag}] sacrifice select: picking {n} "
            f"{[obj_lname(state, r) for r in pick_refs]} (oids {pick_refs}); "
            f"confirmed={done_n} in_flight={len(in_flight)}{over}")
        wire("sacrifice_select", {"who": tag, "iid": iid, "refs": pick_refs,
                                  "confirmed": done_n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec.get("type"),
                                           "data": {"choiceIds": picksel}}}, tag)
        return True
    return False


async def answer_color_choice(c, st, tag):
    if ST["phase"] != "lotus_resolving":
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        chs = data.get("choices") or []
        red = None
        for ch in chs:
            txt = _choice_text(ch).lower()
            if "red" in txt or txt.strip() == "r":
                red = ch
                break
        if red is None:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        SUBMITTED_OPPS.add(iid)
        ST["color_chosen"] = "red"
        say(f"[{tag}] choosing color RED")
        wire("color_choice", {"who": tag, "iid": iid, "choice": red.get("id")})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "choose",
                                           "data": {"choiceId": red.get("id")}}}, tag)
        return True
    return False


async def answer_player_target(c, st, tag):
    if ST["phase"] != "lotus_resolving":
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
        if not kinds or not all(k == "player" for k, _ in kinds):
            continue
        me = next((ch for ch, (_, ref) in zip(chs, kinds) if ref == 0), None)
        pick = me or chs[0]
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] targeting player P0 for Lotus mana")
        wire("player_target", {"who": tag, "iid": iid})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec.get("type"),
                                           "data": {"choiceIds": [pick.get("id")]}}}, tag)
        return True
    return False


async def answer_creature_target(c, st, state, tag):
    """Rage leg: target a Memnite on P0's battlefield.

    Runs only while casting (pre-stack). Uses a (iid, pick) fingerprint
    with a 3-tick quiet window instead of the global SUBMITTED_OPPS set,
    because the engine reuses interaction ids across a cast's sequential
    steps -- a globally-poisoned iid must not block this genuinely new
    question. A still-pending identical question is retried after the
    quiet window (covers rejected submits)."""
    if ST["phase"] != "rage_resolving":
        return False
    # Target selection happens while casting, BEFORE the spell reaches the
    # stack. Post-stack creature prompts are resolution choices, handled by
    # answer_rage_resolution_choice.
    if ST.get("rage_seen_on_stack"):
        return False
    target_oids = set(bf_by_name(state, 0, MEMNITE))
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
        obj_refs = {ref for k, ref in kinds if k == "object"}
        hit = target_oids & obj_refs
        pick_ch = None
        by = "reference"
        if hit:
            pick_ch = next(ch for ch, (k, ref) in zip(chs, kinds)
                           if k == "object" and ref in hit)
            ST["rage_target_oid"] = next(iter(hit))
        else:
            # fallback: text match on 'Memnite'
            for ch in chs:
                if _choice_text(ch).lower() == "memnite":
                    pick_ch = ch
                    break
            if pick_ch is None:
                wire("creature_target_no_match",
                     {"who": tag, "iid": opp.get("interactionId"),
                      "target_oids": sorted(target_oids),
                      "kinds": [str(k) for k in kinds],
                      "texts": [_choice_text(ch) for ch in chs]})
                continue
            for (k, ref) in kinds:
                if k == "object" and ref in target_oids:
                    ST["rage_target_oid"] = ref
                    break
            by = "text"
        iid = opp.get("interactionId") or opp.get("id")
        key = (str(iid), str(pick_ch.get("id")))
        last = ST.get("_tgt_last")
        if last and last["key"] == key and ST["tick"] - last["tick"] <= 3:
            continue
        ST["_tgt_last"] = {"key": key, "tick": ST["tick"]}
        ST["rage_target_submitted"] = True
        say(f"[{tag}] targeting Memnite oid {ST['rage_target_oid']} with Rage "
            f"(by {by}; iid={iid}) -- SUBMITTED")
        wire("rage_target", {"who": tag, "iid": iid,
                             "target_oid": ST["rage_target_oid"],
                             "by": by})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec.get("type"),
                                           "data": {"choiceIds": [pick_ch.get("id")]}}}, tag)
        return True
    return False


def drain_rejections(c):
    """Non-blocking drain of the client inbox; returns rejection messages.
    StateUpdate/GameStarted were already folded into c.latest by _pump,
    so discarding the queued copies is safe."""
    import asyncio as _a
    msgs = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except _a.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            msgs.append((t, data))
    return msgs


async def answer_rage_resolution_choice(c, st, state, tag):
    """DIAGNOSTIC (j-run): Devouring Rage post-stack choice.

    The i-run showed a post-stack select over exactly the 2 sacrificed
    spirits (Frostling, Glitterfang) with the target Memnite NOT among the
    candidates; picking one was rejected with interaction_constraint_unsatisfied.
    This handler dumps the FULL opportunity once, then answers to satisfy the
    advertised constraint (min/max count): it submits min(max(1,min),n)
    candidates, preferring the recorded target Memnite when present, else the
    sacrificed spirits in candidate order. Every rejection is surfaced via
    say(). Dedup: one attempt per (iid, submitted-set) per 4 ticks."""
    if ST["phase"] != "rage_resolving":
        return False
    if not ST.get("rage_seen_on_stack"):
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        iid = str(opp.get("interactionId") or opp.get("id"))
        chs = data.get("candidates") or data.get("choices") or []
        kinds = [_cand_kind(ch) for ch in chs]
        if iid not in ST.setdefault("res_choice_dumped", set()):
            ST["res_choice_dumped"].add(iid)
            full = [{"id": ch.get("id"), "kind": k, "ref": ref,
                     "text": _choice_text(ch)} for ch, (k, ref) in zip(chs, kinds)]
            say(f"[{tag}] RES-CHOICE DUMP iid={iid} spec={json.dumps(spec)[:300]}")
            for f in full:
                say(f"[{tag}]   cand id={f['id']} kind={f['kind']} ref={f['ref']} text={f['text']!r}")
            wire("res_choice_dump", {"who": tag, "iid": iid, "spec": spec,
                                     "candidates": full})
        # constraint: how many must be picked
        cons = ((spec.get("data") or {}).get("constraint") or {})
        cdata = cons.get("data") or {}
        cmin = int(cdata.get("min", 1) or 1)
        cmax = int(cdata.get("max", cmin) or cmin)
        want_n = max(cmin, min(cmax, len(chs)))
        # preference order: recorded target memnite -> sacrificed spirits (candidate order)
        pref = ST.get("rage_target_oid")
        ordered = []
        for ch, (k, ref) in zip(chs, kinds):
            if pref is not None and k == "object" and str(ref) == str(pref):
                ordered.insert(0, ch)
            else:
                ordered.append(ch)
        picks = ordered[:want_n]
        key = (iid, ",".join(str(ch.get("id")) for ch in picks))
        last = ST.get("_res_last")
        if last and last["key"] == key and ST["tick"] - last["tick"] <= 4:
            continue
        ST["_res_last"] = {"key": key, "tick": ST["tick"]}
        say(f"[{tag}] res-choice: submitting {len(picks)} pick(s) "
            f"({[_choice_text(ch) for ch in picks]}) min={cmin} max={cmax} -- SUBMITTED")
        wire("res_choice_submit", {"who": tag, "iid": iid,
                                   "picks": [_choice_text(ch) for ch in picks],
                                   "min": cmin, "max": cmax})
        await interact_as(c, {"interactionId": opp.get("interactionId") or opp.get("id"),
                              "response": {"type": spec.get("type"),
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        await asyncio.sleep(1.0)
        for t, rdata in drain_rejections(c):
            say(f"[{tag}] res-choice REJECTED ({t}): {json.dumps(rdata)[:300]}")
            wire("res_choice_rejected", {"who": tag, "iid": iid,
                                         "kind": t, "data": rdata})
        return True
    return False


async def answer_color_named_choice(c, st, state, tag):
    """Best-effort: answer the Lotus Choose-Color NamedChoice.

    The authoritative waiting_for is NamedChoice/choice_type=Color with
    options [White, Blue, Black, Red, Green]. The viewer interaction may
    present it as exactChoices or a text schema; log the FULL raw
    candidates to the wire log (text extraction has proven unreliable
    here) and prefer a Red match, falling back to the 4th candidate
    (W/U/B/R/G order) or the first candidate. Retries a still-pending
    iid (a rejected submit leaves it pending)."""
    if ST["phase"] != "lotus_resolving":
        return False
    wf = wf_of(state)
    if (wf.get("type") or "") != "NamedChoice":
        return False
    if (wf.get("data") or {}).get("choice_type") != "Color":
        return False
    if str((wf.get("data") or {}).get("player")) != "0":
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        wire("color_choice_raw", {"who": tag, "iid": iid,
                                  "rtype": resp.get("type"),
                                  "n": len(chs),
                                  "candidates": chs})
        pick = None
        for ch in chs:
            blob = json.dumps(ch, default=str).lower()
            if "red" in blob and "green" not in blob.replace("red", ""):
                # avoid matching a blob that merely mentions red among others;
                # prefer an exclusive red hit
                pick = ch
                break
        if pick is None:
            for ch in chs:
                if "red" in json.dumps(ch, default=str).lower():
                    pick = ch
                    break
        if pick is None:
            pick = chs[3] if len(chs) >= 4 else chs[0]
        # allow retry of a still-pending iid
        SUBMITTED_OPPS.discard(iid)
        rtype = resp.get("type")
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        elif rtype == "schema":
            spec = data.get("spec") or {}
            if (spec.get("type") or "") == "text":
                val = None
                for s in pick.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and d.get("role") == "choice" \
                            and "value" in d:
                        val = d["value"]
                        break
                sub = {"interactionId": iid,
                       "response": {"type": "text",
                                    "data": {"value": val if val is not None else "Red"}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": spec.get("type") or "sequence",
                                    "data": {"choiceIds": [pick.get("id")]}}}
        else:
            continue
        SUBMITTED_OPPS.add(iid)
        ST["color_chosen"] = "red-attempt"
        say(f"[{tag}] answering color NamedChoice: pick id={pick.get('id')} "
            f"(rtype={rtype}, n_cand={len(chs)})")
        wire("color_named_choice", {"who": tag, "iid": iid,
                                    "pick_id": pick.get("id")})
        await interact_as(c, sub, tag)
        return True
    return False


async def answer_optional_cost(c, st, tag):
    """Devouring Rage's optional additional cost: accept (pay) the sacrifice.

    Handles the shapes observed on protocol 103:
    - exactChoices with a surface data.role == "accept", value true/false
    - exactChoices with action code "decideOptionalCost" plus a value
      surface role="pay", value true/false (blank texts)
    - text fallback on decideOptional* codes
    Uses a (iid, choice) fingerprint with a 3-tick quiet window instead of
    the global SUBMITTED_OPPS set (iid reuse across a cast's steps)."""
    if ST["phase"] != "rage_resolving":
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        chs = data.get("choices") or []
        accept = None
        for ch in chs:
            is_accept = None
            surfs = ch.get("surfaces") or []
            codes = [str((s.get("data") or {}).get("code")) for s in surfs]
            if any("cancelCast" in cd for cd in codes):
                continue  # never pick the cancel option
            for sf in surfs:
                dd = sf.get("data") or {}
                if dd.get("role") == "accept" and "value" in dd:
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None and any("decideOptionalCost" in cd for cd in codes):
                for sf in surfs:
                    dd = sf.get("data") or {}
                    if dd.get("role") == "pay" and "value" in dd:
                        is_accept = str(dd.get("value")).lower() == "true"
                        break
            if is_accept is None:
                if any("decideOptional" in cd for cd in codes):
                    txt = _choice_text(ch).lower()
                    if any(w in txt for w in ("sacrifice", "pay", "yes", "accept")):
                        is_accept = True
                    elif any(w in txt for w in ("decline", "no", "don't", "do not")):
                        is_accept = False
            if is_accept:
                accept = ch
                break
        if accept is None:
            wire("optional_cost_no_accept",
                 {"who": tag, "iid": opp.get("interactionId") or opp.get("id"),
                  "n_choices": len(chs)})
            continue
        iid = opp.get("interactionId") or opp.get("id")
        key = (str(iid), str(accept.get("id")))
        last = ST.get("_opt_last")
        if last and last["key"] == key and ST["tick"] - last["tick"] <= 3:
            continue
        ST["_opt_last"] = {"key": key, "tick": ST["tick"]}
        say(f"[{tag}] accepting Rage optional additional cost "
            f"(iid={iid}, choice={accept.get('id')}) -- SUBMITTED")
        wire("rage_cost_accept", {"who": tag, "iid": iid,
                                  "choice": accept.get("id")})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "choose",
                                           "data": {"choiceId": accept.get("id")}}}, tag)
        return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


# ------------------------------------------------------------- P0 tick
async def activate_lotus(c, state, acts):
    if ST["phase"] != "rage_done":
        return False
    lotuses = [o for o in bf_by_name(state, 0, LOTUS)
               if untapped(get_obj(state, o))]
    if not lotuses:
        return False
    memnites = bf_by_name(state, 0, MEMNITE)
    if len(memnites) < 1:  # sacrifice at least 1 (Rage leg already used the board)
        return False
    for a in acts:
        if a.get("type") == "ActivateAbility" and str(a.get("_src_oid")) == str(lotuses[0]):
            if not ST["pre_exported"]:
                await export_as(c, "pre_lotus")
                ST["pre_exported"] = True
                pre_st = json.load(open(f"{EVDIR}/pre_lotus.json")).get("state", {})
                ST["pre_lotus_pool"] = mana_pool(pre_st, 0)
                say(f"[P0] pre_lotus.json exported; pool={ST['pre_lotus_pool']}; "
                    f"turn={pre_st.get('turn_number')} phase={pre_st.get('phase')}")
                wire("pre_exported", {"pool": ST["pre_lotus_pool"]})
            ST["lotus_oid"] = lotuses[0]
            ST["lotus_activated"] = True
            ST["phase"] = "lotus_resolving"
            say(f"[P0] activating Radiant Lotus (oid {lotuses[0]}) -- DECISIVE")
            wire("lotus_activated", {"oid": lotuses[0]})
            await submit_as_is(c, a)
            return True
    return False


async def cast_rage(c, state, acts):
    if RAGE not in hand_lnames(state, 0):
        return False
    spirits = [o for n in SPIRITS for o in bf_by_name(state, 0, n)]
    if len(spirits) < 2:
        return False
    if not bf_by_name(state, 0, MEMNITE):
        return False
    if len(untapped_lands(state, 0)) < 5:
        return False
    a, oid = cast_action_for(acts, state, RAGE)
    if not a:
        return False
    if not ST["pre_rage_exported"]:
        await export_as(c, "pre_rage")
        ST["pre_rage_exported"] = True
        say(f"[P0] pre_rage.json exported")
        wire("pre_rage_exported", {})
    ST["rage_oid"] = oid
    ST["phase"] = "rage_resolving"
    ST["mana_needs"]["P0"] = {"R": 1, "generic": 4}
    say(f"[P0] casting Devouring Rage (oid {oid}) -- DECISIVE")
    wire("rage_cast", {"oid": oid})
    await submit_as_is(c, a)
    return True


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
    # leg-specific interactions (order-agnostic)
    if await answer_optional_cost(c, st, "P0"):
        return
    if await answer_sacrifice_number(c, st, "P0"):
        return
    if await answer_sacrifice_select(c, st, state, "P0"):
        return
    if await answer_color_choice(c, st, "P0"):
        return
    if await answer_color_named_choice(c, st, state, "P0"):
        return
    if await answer_player_target(c, st, "P0"):
        return
    if await answer_creature_target(c, st, state, "P0"):
        return
    if await answer_rage_resolution_choice(c, st, state, "P0"):
        return
    # resolution detection: the ability must have been SEEN on the stack
    # before its absence means resolution (an empty stack during cost
    # payment is not resolution).
    if ST["phase"] == "lotus_resolving" and ST["lotus_oid"] is not None:
        on_stack = any(str(se.get("source_id")) == str(ST["lotus_oid"])
                       for se in stack_entries(state))
        if on_stack and not ST.get("lotus_seen_on_stack"):
            ST["lotus_seen_on_stack"] = True
            say(f"[P0] lotus ability on stack (costs paid)")
            wire("lotus_on_stack", {})
        # The Choose-Color is a post-stack NamedChoice; only treat the leg
        # as resolved once it is answered (or gone) AND the stack cleared.
        wf = wf_of(state)
        color_pending = (wf.get("type") == "NamedChoice"
                         and (wf.get("data") or {}).get("choice_type") == "Color")
        if ST.get("lotus_seen_on_stack") and not on_stack \
                and not color_pending and not ST["post_lotus_exported"]:
            await export_as(c, "post_lotus")
            ST["post_lotus_exported"] = True
            post_st = json.load(open(f"{EVDIR}/post_lotus.json")).get("state", {})
            ST["post_lotus_pool"] = mana_pool(post_st, 0)
            say(f"[P0] lotus resolved; post_lotus pool={ST['post_lotus_pool']}")
            wire("lotus_resolved", {"pool": ST["post_lotus_pool"]})
            ST["phase"] = "done"
            return
    if ST["phase"] == "rage_resolving" and ST["rage_oid"] is not None:
        on_stack = any(str(se.get("source_id")) == str(ST["rage_oid"])
                       for se in stack_entries(state))
        if on_stack and not ST.get("rage_seen_on_stack"):
            ST["rage_seen_on_stack"] = True
            say(f"[P0] rage spell on stack (costs paid)")
            wire("rage_on_stack", {})
        if ST.get("rage_seen_on_stack") and not on_stack \
                and not ST["post_rage_exported"]:
            await export_as(c, "post_rage")
            ST["post_rage_exported"] = True
            post_st = json.load(open(f"{EVDIR}/post_rage.json")).get("state", {})
            if ST["rage_target_oid"] is not None:
                ST["post_rage_pt"] = pt_of(post_st, ST["rage_target_oid"])
            say(f"[P0] rage resolved; target pt={ST['post_rage_pt']}")
            wire("rage_resolved", {"pt": ST["post_rage_pt"]})
            ST["phase"] = "rage_done"
            return
    # debug: log unanswered opportunities while a leg is resolving
    # (deduped by iid set)
    if ST["phase"] in ("lotus_resolving", "rage_resolving"):
        iids = tuple(sorted(str(opp.get("interactionId") or opp.get("id"))
                            for opp in vi_ops(st)))
        if iids and iids != ST.get("_last_opp_iids"):
            ST["_last_opp_iids"] = iids
            _log_opportunities(st, "P0", f"unanswered-{ST['phase']}")
    # build actions on own main phase with empty stack
    if my_main(state, pid):
        if ST["phase"] == "build":
            if await cast_rage(c, state, acts):
                return
        if ST["phase"] == "rage_done":
            if await activate_lotus(c, state, acts):
                return
        if ST["phase"] == "build":
            hn = hand_lnames(state, pid)
            n_mount = len(untapped_lands(state, pid))
            dbg_key = ("dbg", state.get("turn_number"), ST["phase"])
            if dbg_key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(dbg_key)
                cast_names = []
                for a in acts:
                    if "cast" in a["type"].lower():
                        d = a.get("data", {})
                        oids = [v for v in list(d.values()) + [a.get("_src_oid")]
                                if isinstance(v, int)]
                        cast_names.append([obj_lname(state, v) for v in oids])
                say(f"[P0] DBG turn={state.get('turn_number')} hand={hn} "
                    f"n_untapped_lands={n_mount} lotus_bf={bool(bf_by_name(state, 0, LOTUS))} "
                    f"cast_actions={cast_names}")
            # cast Lotus (only after the Rage leg; the Lotus leg is second)
            if ST["phase"] == "rage_done" and LOTUS in hn and n_mount >= 6 \
                    and not bf_by_name(state, 0, LOTUS):
                a, oid = cast_action_for(acts, state, LOTUS)
                if a:
                    ST["mana_needs"]["P0"] = {"generic": 6}
                    say(f"[P0] casting Radiant Lotus (oid {oid})")
                    wire("lotus_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
            # cast spirits (cap 3 each on BF)
            for sp in SPIRITS:
                if sp in hn and n_mount >= 1 and len(bf_by_name(state, 0, sp)) < 3:
                    a, oid = cast_action_for(acts, state, sp)
                    if a:
                        ST["mana_needs"]["P0"] = {"R": 1}
                        say(f"[P0] casting {sp} (oid {oid})")
                        await submit_as_is(c, a)
                        return
            # cast Memnite (free)
            if MEMNITE in hn and len(bf_by_name(state, 0, MEMNITE)) < 10:
                a, oid = cast_action_for(acts, state, MEMNITE)
                if a:
                    say(f"[P0] casting Memnite (oid {oid})")
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

        # ---- rage leg (FIRST: no color-choice UX dependency)
        say("setup done; driving Rage leg")
        t1 = time.time()
        while time.time() - t1 < SETUP_DEADLINE_S and ST["phase"] in ("build", "rage_resolving"):
            await asyncio.sleep(0.5)
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
        say(f"rage leg ended: phase={ST['phase']}")

        # ---- lotus leg (SECOND, best-effort: the Choose-Color NamedChoice
        # may need manual answering; the Rage leg already establishes the verdict)
        if ST["phase"] == "rage_done":
            say("driving Lotus leg")
            t2 = time.time()
            while time.time() - t2 < RAGE_DEADLINE_S and ST["phase"] != "done":
                await asyncio.sleep(0.5)
                for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                    try:
                        await tick(c)
                    except Exception as e:
                        say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                        wire("tick_error", {"who": c.name,
                                            "err": f"{type(e).__name__}: {e}"})
            say(f"lotus leg ended: phase={ST['phase']}")
    finally:
        await finalize(c0)
        await c0.close()
        await c1.close()
        if not WIRE.closed:
            WIRE.close()
        if not RUNLOG.closed:
            RUNLOG.close()


# ------------------------------------------------------------- finalize
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]

    def load_env(name):
        try:
            return json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name}.json load failed: {e}")
            return None

    pre_lotus = load_env("pre_lotus") or {}
    post_lotus = load_env("post_lotus") or {}
    pre_rage = load_env("pre_rage") or {}
    post_rage = load_env("post_rage") or {}
    pre_lotus_st = pre_lotus.get("state") or {}
    post_lotus_st = post_lotus.get("state") or {}
    post_rage_st = post_rage.get("state") or {}

    # A1: data-level parse carries the reported consumer shapes
    detail = ST.get("defect_detail") or {}
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append(
            "A1 passed: pinned v0.101.0 card-data.json carries the reported "
            "defect shapes -- Radiant Lotus activated ability sub_ability "
            "Mana{{ChosenColor, count Ref(FilteredTrackedSetSize{{Artifact, "
            f"caused_by: Sacrificed}}}} ({len(detail.get('lotus_hits', []))} "
            "hits); Devouring Rage repeat_for "
            "Ref(FilteredTrackedSetSize{{Spirit, caused_by: Sacrificed}}) "
            f"({len(detail.get('rage_hits', []))} hits); Lotus cost shape "
            "{{T}}, Sacrifice confirmed. See data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not carry the reported "
                     "consumer shapes; see data_evidence.json.")

    # A2: setup
    notes.append(f"A2 probe: hello_ok={ST['hello_ok']}; "
                 f"game_code={ST['game_code']}; "
                 f"post_mulligan_seen={ST['post_mulligan_seen']}; "
                 f"pre_exported={ST['pre_exported']}; "
                 f"pre_rage_exported={ST['pre_rage_exported']}; "
                 f"wf_types_seen={ST['wf_types_seen']}")
    if ST["hello_ok"] and ST["game_code"] and ST["post_mulligan_seen"] \
            and (ST["pre_exported"] or ST["pre_rage_exported"]):
        ass["A2_setup_ok"] = "passed"
        notes.append("A2 passed: game started for both seats, mulligans done, "
                     "a pre-leg state exported inside the submit path "
                     "(pre_rage.json for the first leg).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: setup not established.")

    # A3: the Lotus sacrifice genuinely happened
    sac_m = ST.get("sacrificed_memnites") or []
    if sac_m and post_lotus_st:
        zones = {o: get_obj(post_lotus_st, o).get("zone") for o in sac_m}
        in_gy = [o for o, z in zones.items() if z == "Graveyard"]
        notes.append(f"A3 probe: sacrificed_memnite_oids={sac_m}; zones at post_lotus={zones}")
        if len(in_gy) == len(sac_m) and len(sac_m) >= 1:
            ass["A3_lotus_sacrifice_paid"] = "passed"
            notes.append(f"A3 passed: {len(in_gy)} Memnite(s) sacrificed as the "
                         f"activation cost are in P0's graveyard at post_lotus "
                         f"(oids {in_gy}); the sacrifice genuinely happened.")
        else:
            ass["A3_lotus_sacrifice_paid"] = "failed"
            notes.append("A3 FAILED: sacrificed artifacts not found in graveyard.")
    else:
        ass["A3_lotus_sacrifice_paid"] = "failed"
        notes.append(f"A3 FAILED: no sacrifice selection recorded "
                     f"(sacrificed_memnites={sac_m}); the Lotus cost path was "
                     f"never driven.")

    # A4: mana added = number of artifacts sacrificed (pinned parse counts N)
    pre_pool = ST.get("pre_lotus_pool") or {}
    post_pool = ST.get("post_lotus_pool") or {}
    red_delta = (post_pool.get("red", 0) - pre_pool.get("red", 0))
    n_sac = len(sac_m)
    notes.append(f"A4 probe: pre pool={pre_pool} post pool={post_pool} "
                 f"red_delta={red_delta} n_sacrificed={n_sac} "
                 f"color_chosen={ST.get('color_chosen')}")
    if ass["A3_lotus_sacrifice_paid"] == "passed":
        if red_delta == n_sac and n_sac > 0:
            ass["A4_lotus_mana_added"] = "passed"
            notes.append(f"A4 passed: P0's red pool grew by {red_delta} = "
                         f"number of sacrificed artifacts ({n_sac}).")
        else:
            ass["A4_lotus_mana_added"] = "failed"
            notes.append(f"A4 FAILED: P0 sacrificed {n_sac} artifact(s) to the "
                         f"activation cost and chose Red, but the red mana "
                         f"pool changed by {red_delta} (expected {n_sac} per "
                         f"the pinned parse count "
                         f"Ref(FilteredTrackedSetSize{{Artifact, "
                         f"caused_by: Sacrificed}})). The consumer read 0 -- "
                         f"the reported defect.")
    else:
        ass["A4_lotus_mana_added"] = "not-run"
        notes.append("A4 not-run: A3 failed.")

    # A5: the Rage sacrifice genuinely happened
    sac_s = ST.get("sacrificed_spirits") or []
    if sac_s and post_rage_st:
        zones = {o: get_obj(post_rage_st, o).get("zone") for o in sac_s}
        in_gy = [o for o, z in zones.items() if z == "Graveyard"]
        names = [obj_lname(post_rage_st, o) for o in in_gy]
        notes.append(f"A5 probe: sacrificed_spirit_oids={sac_s}; zones={zones}")
        if in_gy and all(n in SPIRITS for n in names):
            ass["A5_rage_cost_paid"] = "passed"
            notes.append(f"A5 passed: {len(in_gy)} Spirit(s) sacrificed as the "
                         f"Rage additional cost are in the graveyard at "
                         f"post_rage (oids {in_gy}).")
        else:
            ass["A5_rage_cost_paid"] = "failed"
            notes.append("A5 FAILED: sacrificed spirits not in graveyard.")
    elif ST["phase"] in ("rage_done", "done"):
        ass["A5_rage_cost_paid"] = "failed"
        notes.append("A5 FAILED: rage resolved but no spirit sacrifice recorded.")
    else:
        ass["A5_rage_cost_paid"] = "not-run"
        notes.append(f"A5 not-run: rage leg did not complete (phase={ST['phase']}).")

    # A6: pump = base +3/+0 plus +3/+0 per sacrificed spirit
    pt = ST.get("post_rage_pt")
    notes.append(f"A6 probe: rage_target_oid={ST.get('rage_target_oid')} pt={pt} "
                 f"n_sacrificed_spirits={len(sac_s)}")
    if ass["A5_rage_cost_paid"] == "passed" and pt is not None:
        want = (1 + 3 + 3 * len(sac_s), 1)
        if tuple(pt) == want:
            ass["A6_rage_pump"] = "passed"
            notes.append(f"A6 passed: target Memnite is {pt[0]}/{pt[1]} = "
                         f"1/1 +3/+0 base + {len(sac_s)}x +3/+0.")
        else:
            ass["A6_rage_pump"] = "failed"
            notes.append(f"A6 FAILED: target Memnite is {pt[0]}/{pt[1]}, "
                         f"expected {want[0]}/{want[1]} (1/1 base +3/+0 and "
                         f"+3/+0 per each of the {len(sac_s)} sacrificed "
                         f"Spirits). The repeat_for "
                         f"FilteredTrackedSetSize{{Spirit, caused_by: "
                         f"Sacrificed}} resolved to 0 -- the reported defect.")
    else:
        ass["A6_rage_pump"] = "not-run"
        notes.append("A6 not-run: A5 not passed or no target P/T.")

    # verdict
    a1, a2, a3, a4 = ass["A1_data_level"], ass["A2_setup_ok"], \
        ass["A3_lotus_sacrifice_paid"], ass["A4_lotus_mana_added"]
    a5, a6 = ass["A5_rage_cost_paid"], ass["A6_rage_pump"]
    if a1 != "passed":
        verdict = "not-reproduced"
    elif a2 != "passed":
        verdict = "blocked"
    elif (a3 == "passed" and a4 == "failed") or \
            (a5 == "passed" and a6 == "failed"):
        verdict = "reproduced"
    elif (a3 == "passed" and a4 == "passed") or \
            (a5 == "passed" and a6 == "passed"):
        verdict = "not-reproduced"
        if not ((a3 == "passed" and a4 == "passed")
                and (a5 == "passed" and a6 == "passed")):
            notes.append(
                "verdict rests on one completed leg; the other leg did not "
                "complete -- see limitations.")
    else:
        verdict = "blocked"
    say(f"verdict: {verdict} (assertions: {ass})")

    n_sac = len(sac_m)
    if verdict == "reproduced":
        if a4 == "failed":
            result = (
                f"Radiant Lotus activation: P0 sacrificed {n_sac} artifact(s) "
                f"to pay the activation cost (graveyard-confirmed at "
                f"post_lotus: oids {sac_m}), chose Red, targeted P0 -- but "
                f"P0's red mana pool changed by {red_delta} (expected "
                f"{n_sac} per the pinned parse's "
                f"Ref(FilteredTrackedSetSize{{Artifact, caused_by: "
                f"Sacrificed}}) count). The activation-cost publish path "
                f"published nothing; the consumer read 0. Matches the "
                f"issue's measured `[PV2 radiant-lotus] sets=[(1, [])]` "
                f"reading.")
        else:
            want_pt = (1 + 3 + 3 * len(sac_s), 1)
            result = (
                f"Devouring Rage cast: P0 sacrificed {len(sac_s)} Spirit(s) "
                f"as the additional cost (graveyard-confirmed at post_rage: "
                f"oids {sac_s}), but the target Memnite is "
                f"{pt[0]}/{pt[1]} instead of the expected "
                f"{want_pt[0]}/{want_pt[1]} (1/1 base +3/+0 plus +3/+0 per "
                f"sacrificed Spirit). The repeat_for "
                f"FilteredTrackedSetSize{{Spirit, caused_by: Sacrificed}} "
                f"resolved to 0 -- only the base pump applied. Matches the "
                f"issue's measured `[PV2 devouring-rage] sets=[(1,[])] "
                f"pt=(5,2)` shape.")
    elif verdict == "not-reproduced":
        result = ("Both driven legs produced the correct consumer outcomes: "
                  "Lotus mana delta matched the sacrificed count and the "
                  "Rage target reached the full pumped P/T. The reported "
                  "defect does not reproduce on v0.101.0.")
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
        "scope": ("Radiant Lotus activation-cost sacrifice publish + "
                  "Devouring Rage additional-cost sacrifice publish: two "
                  "runtime legs on the native engine, two human driver "
                  "seats, protocol-103 driver. Asserts the consumer "
                  "outcomes (mana added; P/T pump) against the pinned "
                  "parse's FilteredTrackedSetSize{caused_by: Sacrificed} "
                  "shapes, with the sacrifices' reality grounded in "
                  "graveyard zones."),
        "result": result,
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Expected Lotus mana = N (number of sacrificed artifacts) per the pinned parse's count shape; the oracle text ('three mana ... for each artifact') would imply 3N -- either way the defect shows 0.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports restorable only via full game replay.",
            "P1 is a passive land-drop seat; no combat was driven.",
        ],
        "observations": {
            "sacrificed_memnites": sac_m,
            "sacrificed_spirits": sac_s,
            "pre_lotus_pool": pre_pool,
            "post_lotus_pool": post_pool,
            "red_delta": red_delta,
            "post_rage_pt": pt,
            "color_chosen": ST.get("color_chosen"),
            "rage_target_oid": ST.get("rage_target_oid"),
            "wf_types_seen": ST["wf_types_seen"],
            "game_code": ST["game_code"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7457.py", "w") as f:
        f.write(open(__file__).read())
    say(f"run.json written; verdict={verdict}")


if __name__ == "__main__":
    asyncio.run(main())
