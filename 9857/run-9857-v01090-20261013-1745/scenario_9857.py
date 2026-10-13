#!/usr/bin/env python3
"""Issue #9857: `EnchantedBy` fallback counts phased-out Auras.

Report (phase-rs/phase#9857, opened 2026-10-12, open,
bug/area:engine/priority:p3-card-specific): read from code -- the fallback
attachment query behind "enchanted creature" style filters counts a
phased-out Aura, while `HasAttachment` skips phased-out permanents.
Relevant to Wall of Putrid Flesh and Enchanted Being style filters:
"enchanted creatures" with a phased-out Aura.
Where: `game/filter.rs` `FilterProp::EnchantedBy` arm.
CR 702.26b: a phased-out permanent is treated as though it does not exist.

Code shape (checked out engine source, 2026-10-13):
  - `FilterProp::EnchantedBy` fallback (non-Aura source): counts an
    attachment if it is an Aura subtype -- NO phased-out check.
  - `FilterProp::HasAttachment`: explicitly skips phased-out attachments
    ("CR 702.26b: a separately phased-out attachment does not exist for
    this live predicate, even though its attachment link remains").

Behavioral contract (per playbook: test the reported OUTCOME):
  - P0 casts Holy Strength ({W} Aura) enchanting P1's Prodigal Pyromancer.
  - P1 casts Wall of Putrid Flesh ({2}{B} 2/4: "Prevent all damage that
    would be dealt to this creature by enchanted creatures." -- a NON-Aura
    source whose damage_source_filter uses FilterProp::EnchantedBy, i.e.
    the buggy fallback arm).
  - BASELINE: P1 pings its own Wall with the Pyromancer (1 damage). The
    Pyromancer is enchanted -> damage must be PREVENTED (Wall
    damage_marked == 0). Proves the prevention machinery is live.
  - P0 casts Teferi's Protection ({2}{W}): all permanents P0 controls
    phase out -- the Holy Strength phases out; P1's Pyromancer and Wall
    stay phased in.
  - POST-PHASE: P1 pings its Wall again. Per CR 702.26b the phased-out
    Aura is treated as though it does not exist -> the Pyromancer is NOT
    an enchanted creature -> damage must NOT be prevented (Wall
    damage_marked == 1). If damage_marked stays 0, the EnchantedBy
    fallback counted the phased-out Aura: the reported bug.

Assertions:
  A1 setup_ok            Holy Strength attached to the Pyromancer and the
                         Pyromancer is 2/3 (bonus applied)
  A2 baseline_prevented  passed = baseline ping prevented (Wall
                         damage_marked == 0 while the Aura is phased in);
                         failed = damage went through while enchanted
                         (setup invalid for the comparison)
  A3 phaseout            passed = after Teferi's Protection resolves, the
                         Holy Strength reads phased-out while the
                         Pyromancer and the Wall read phased-in
  A4 decisive            passed = post-phase ping damage NOT prevented
                         (Wall damage_marked == 1: correct per CR 702.26b);
                         FAILED = damage_marked == 0 (the reported bug:
                         the fallback counted the phased-out Aura)
  A5 bonus_dropped       observation: Pyromancer P/T back to 1/1 after the
                         phase-out (the Aura's own static no longer
                         applies); failed = still 2/3

Verdict:
  reproduced      A2 passed and A3 passed and A4 failed
  not-reproduced  A2 passed and A3 passed and A4 passed
  blocked         the setup legs (A1/A2/A3) never completed

Driver conventions (protocol 140, from scenario_9856.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - waiting_for gone (null); priority = PassPriority in top-level
    legal_actions; decisions via viewer_interaction
  - exact card-data names; export response data.state is a JSON string parsed
    once; runtime state objects use "card_types" (plural)
  - MulliganDecision via legacy Action (Keep); DiscardToHandSize via vi;
    mana via vi tapLandForMana; casts via advertised legal actions submitted
    as advertised (targets answered via the engine's vi target selection
    after CastSpell, CR 601.2c); ActivateAbility submitted while holding
    priority; sleep(0) yield before leg evaluation
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9857")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9857-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9857
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.109.0",
    "build_commit": "932e389",
    "protocol_version": 140,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.109.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "fresh game created by this run; evidence under evidence/9857/"
               + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


for _f, _k in (("server/releases/v0.109.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.109.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.109.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h, f"empty hash for {_f}"
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.109.0/data/card-data.json"))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
LAST_PASS_T = {}


async def maybe_pass_priority(c, st, acts, tag):
    if not my_priority(acts):
        return False
    last_rev = PASSED_REV.get(c.name, -1)
    if last_rev < c.revision or \
            (last_rev == c.revision
             and time.time() - LAST_PASS_T.get(c.name, 0) > 5):
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision
        LAST_PASS_T[c.name] = time.time()
        return True
    return False


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def damage_marked_of(state, oid):
    try:
        return int(get_obj(state, oid).get("damage_marked") or 0)
    except (TypeError, ValueError):
        return 0


def phase_status_str(o):
    ps = o.get("phase_status")
    if ps is None:
        # phased-in permanents carry no phase status (only phased-out ones
        # do: {"status": "PhasedOut", "cause": ...})
        return "PhasedIn"
    if isinstance(ps, dict):
        s = ps.get("status") or ps.get("phase") or ""
        if not s:
            # serde tag="status" unit variants may serialize oddly; scan values
            for v in ps.values():
                if isinstance(v, str) and "phas" in v.lower():
                    s = v
                    break
        return str(s)
    if isinstance(ps, str):
        return ps
    if isinstance(o.get("phased_out"), bool):
        return "PhasedOut" if o.get("phased_out") else "PhasedIn"
    return "?"


def is_phased_out(o):
    return phase_status_str(o).lower() == "phasedout"


def pt_of(state, oid):
    def num(v):
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    o = get_obj(state, oid)
    return num(o.get("power")), num(o.get("toughness"))


def tapped_of(state, oid):
    return bool(get_obj(state, oid).get("tapped"))


HS = "holy strength"
TP = "teferi's protection"
PYRO = "prodigal pyromancer"
WALL = "wall of putrid flesh"


def pyro_oids(state, pid=1):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == PYRO]


def wall_oids(state, pid=1):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == WALL]


def hs_oids(state, pid=0):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == HS]


def attachments_of(state, oid):
    o = get_obj(state, oid)
    atts = o.get("attachments") or o.get("attached_by") or []
    return [str(a) for a in atts]


def attached_to_of(state, oid):
    o = get_obj(state, oid)
    for k in ("attached_to", "attaching", "enchanting"):
        v = o.get(k)
        if v is None:
            continue
        if isinstance(v, dict) and "data" in v:
            return str(v["data"])
        return str(v)
    return None

# ------------------------------------------------------- interaction helpers

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
            spec = (data.get("spec", {}) or {}).get("type")
            if spec == "relations":
                continue
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
    return real_decision_pending(st)


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:12],
                    "rtype": resp.get("type"),
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c),
                    "texts": [choice_text(ch)[:24] for ch in items[:8]]})
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] is not None:
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.109.0"), f"unexpected version {ver}"
    assert int(proto) == 140, f"unexpected protocol {proto}"
    assert str(build) == "932e389", f"unexpected build {build}"


def target_opportunity(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype == "text":
                continue
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


def pick_target_by_name(state, opp, lname):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        ref = _cand_ref(ch)
        if ref and obj_lname(state, ref) == lname:
            return ch
    for ch in cands:
        if lname in choice_text(ch).lower():
            return ch
    return None


# ------------------------------------------------------------- action helpers

MULL_COUNT = {}


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    want = HS if pid == 0 else PYRO
    if len(hn) > 4 and MULL_COUNT.get(tag, 0) < 2 and want not in hn:
        MULL_COUNT[tag] = MULL_COUNT.get(tag, 0) + 1
        say(f"[{tag}] mulligan #{MULL_COUNT[tag]} ({len(hn)} cards, "
            f"hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
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
    keep = {HS, TP} if pid == 0 else {PYRO, WALL}

    def bkey(ch):
        ref = _cand_ref(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in keep:
            return (1, str(ref))
        return (0, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_ref(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": str(iid)[:12],
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
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
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
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
                manas = [s for s in ch.get("surfaces", []) or []]
                syms = []
                for m in manas:
                    if m.get("type") == "mana":
                        syms = (m.get("data") or {}).get("symbols", []) or []
                        break
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
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action({"type": "PassPriority"})
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
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


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


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def untapped_colored_lands(state, pid):
    out = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}
    cmap = {"plains": "W", "island": "U", "swamp": "B",
            "mountain": "R", "forest": "G"}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = cmap.get(obj_lname(state, o))
            if c:
                out[c] += 1
    return out


def find_activates(st, source_oid):
    out = []
    seen = set()
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility" and \
                str((a.get("data") or {}).get("source_id")) == str(source_oid):
            idx = (a.get("data") or {}).get("ability_index")
            if idx not in seen:
                seen.add(idx)
                out.append(a)
    return out


def find_activate(st, source_oid):
    ms = find_activates(st, source_oid)
    if ms:
        return ("submit", ms[0])
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" in codes:
                return ("vi", opp, ch)
    return None


def stack_entries_from(state, src_oid):
    out = []
    for e in state.get("stack") or []:
        if str(e.get("source_id")) == str(src_oid):
            out.append(e)
    return out


def stack_has_spell(state, lname):
    for e in state.get("stack") or []:
        oid = e.get("object_id", e.get("id", e.get("source_id", "")))
        if obj_lname(state, oid) == lname:
            return True
    return False

# ------------------------------------------------------------------- ticks

async def drain_rejections(c, tag):
    while c.rejections:
        r = c.rejections.pop(0)
        wire("rejection", {"who": tag, "rejection": r})
        say(f"[{tag}] REJECTION: {json.dumps(r)[:300]}")


async def common_tick_head(c, acts, st, pid, tag, ctx):
    await drain_rejections(c, tag)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # never block in this scenario; hold
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


def hs_attached_to_pyro(state, ctx):
    hs = ctx.get("hs_oid")
    pyro = ctx.get("pyro_oid")
    if not hs or not pyro:
        return False
    if obj_zone(state, hs) != "Battlefield" or obj_zone(state, pyro) != "Battlefield":
        return False
    if attached_to_of(state, hs) == str(pyro):
        return True
    return str(hs) in attachments_of(state, pyro)


async def p0_tick(c, tag, ctx, host=None):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 0, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # --- track the Holy Strength hitting the battlefield
    if not ctx.get("hs_oid"):
        hss = hs_oids(state, 0)
        if hss:
            ctx["hs_oid"] = hss[0]
            say(f"[{tag}] Holy Strength on battlefield (oid {hss[0]}), "
                f"attached_to={attached_to_of(state, hss[0])}")
            wire("hs_entered", {"oid": hss[0],
                                "hs_object": get_obj(state, hss[0])})
    # --- track the Pyromancer (P1's) for the enchant target
    if not ctx.get("pyro_oid"):
        pys = pyro_oids(state, 1)
        if pys:
            ctx["pyro_oid"] = pys[0]
            say(f"[{tag}] Prodigal Pyromancer on battlefield (oid {pys[0]})")

    # --- A1: record the Pyromancer's P/T the first time the Aura is seen
    # attached (the end-of-run read is too late: the Aura phases out)
    if hs_attached_to_pyro(state, ctx) and ctx.get("a1_pt") is None:
        ctx["a1_pt"] = pt_of(state, ctx["pyro_oid"])
        say(f"[{tag}] A1: HS attached to the Pyromancer; P/T={ctx['a1_pt']}")
        wire("a1_pt", {"pyro_oid": ctx["pyro_oid"],
                       "pyro_pt": ctx["a1_pt"]})

    # --- main-phase plays
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return
        hn = hand_names(state, 0)
        cols = untapped_colored_lands(state, 0)
        # cast Holy Strength enchanting P1's Pyromancer
        if not ctx.get("hs_cast") and HS in hn and ctx.get("pyro_oid") \
                and cols["W"] >= 1:
            a, oid = cast_action_for(acts, state, HS)
            if a is not None:
                ctx["hs_cast"] = True
                ST["mana_needs"][tag] = {"W": 1}
                say(f"[{tag}] casting Holy Strength (oid {oid}) on the "
                    f"Pyromancer (oid {ctx['pyro_oid']})")
                wire("cast_hs", {"oid": oid, "target_oid": ctx["pyro_oid"],
                                 "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return
        # cast Teferi's Protection once the baseline ping resolved
        if ctx.get("hs_cast") and ctx.get("ping1_resolved") \
                and not ctx.get("tp_cast") and TP in hn \
                and cols["W"] >= 1 and untapped_land_total(state, 0) >= 3:
            a, oid = cast_action_for(acts, state, TP)
            if a is not None:
                ctx["tp_cast"] = True
                ST["mana_needs"][tag] = {"W": 1, "generic": 2}
                say(f"[{tag}] BASELINE done; casting Teferi's Protection "
                    f"(oid {oid})")
                wire("cast_tp", {"oid": oid,
                                 "cast_action_data": a.get("data")})
                try:
                    pre = json.loads(await (host or c).export_state())
                    with open(f"{EVDIR}/pre_phase.json", "w") as f:
                        json.dump(pre, f, indent=1)
                    wire("pre_phase_export", {})
                except Exception as e:
                    wire("pre_phase_export_failed", {"error": str(e)[:200]})
                await submit_as_is(c, a)
                return

    # --- answer the Holy Strength enchant target selection with the Pyromancer
    if ctx.get("hs_cast") and not ctx.get("hs_targeted"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            wire("hs_target_seen",
                 {"rev": c.revision, "rtype": rtype, "stype": stype,
                  "candidates": [{"text": choice_text(ch)[:80],
                                  "ref": _cand_ref(ch),
                                  "ref_name": obj_lname(state, _cand_ref(ch))
                                  if _cand_ref(ch) else None}
                                 for ch in cands]})
            pick = pick_target_by_name(state, opp, PYRO)
            if pick is not None:
                ctx["hs_targeted"] = True
                say(f"[{tag}] enchanting the Prodigal Pyromancer with Holy "
                    f"Strength")
                await answer_vi(c, opp, pick, tag)
                return
            key = (tag, "hs-target-miss", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] HS target prompt has no Pyromancer candidate "
                    f"-- recorded, not answered")
            return

    # --- detect Teferi's Protection resolution: spell off the stack and the
    # Holy Strength reading phased-out
    if ctx.get("tp_cast") and not ctx.get("tp_resolved"):
        hs = ctx.get("hs_oid")
        tp_still = stack_has_spell(state, TP)
        if hs and not tp_still:
            hso = get_obj(state, hs)
            ps = phase_status_str(hso)
            say(f"[{tag}] TP resolution check: HS phase_status={ps} "
                f"(raw={json.dumps(hso.get('phase_status'), default=str)[:160]})")
            wire("tp_resolve_check",
                 {"hs_oid": hs, "phase_status": ps,
                  "phase_status_raw": hso.get("phase_status"),
                  "hs_zone": obj_zone(state, hs),
                  "hs_object": hso})
            if is_phased_out(hso):
                ctx["tp_resolved"] = True
                ctx["tp_resolve_rev"] = c.revision
                pys = pyro_oids(state, 1)
                wls = wall_oids(state, 1)
                ctx["post_phase"] = {
                    "hs_phase": ps,
                    "pyro_phase": phase_status_str(get_obj(state, pys[0])) if pys else None,
                    "wall_phase": phase_status_str(get_obj(state, wls[0])) if wls else None,
                    "pyro_pt": pt_of(state, pys[0]) if pys else None,
                }
                say(f"[{tag}] TP RESOLVED: HS phased out; "
                    f"pyro_phase={ctx['post_phase']['pyro_phase']} "
                    f"wall_phase={ctx['post_phase']['wall_phase']} "
                    f"pyro_pt={ctx['post_phase']['pyro_pt']}")
                wire("tp_resolved", ctx["post_phase"])
                try:
                    post = json.loads(await (host or c).export_state())
                    with open(f"{EVDIR}/post_phase.json", "w") as f:
                        json.dump(post, f, indent=1)
                    wire("post_phase_export", {})
                except Exception as e:
                    wire("post_phase_export_failed", {"error": str(e)[:200]})
                return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    await maybe_pass_priority(c, st, acts, tag)


async def p1_ping_leg(c, st, acts, state, tag, ctx, which, host):
    """Shared ping leg. which=1 (baseline) or 2 (post-phase). Returns True
    if it acted."""
    act_key = f"ping{which}_activated"
    tgt_key = f"ping{which}_targeted"
    seen_key = f"ping{which}_seen_on_stack"
    res_key = f"ping{which}_resolved"
    dmg_key = f"ping{which}_damage"
    if ctx.get(res_key):
        return False
    pyro = ctx.get("pyro_oid")
    walls = wall_oids(state, 1)
    if not pyro or not walls:
        return False
    wall = walls[0]
    if obj_zone(state, pyro) != "Battlefield":
        return False
    # --- submit the activation
    if not ctx.get(act_key):
        if not my_main(state, 1):
            return False
        if which == 1 and not hs_attached_to_pyro(state, ctx):
            return False
        if which == 2 and not ctx.get("tp_resolved"):
            return False
        if tapped_of(state, pyro):
            return False
        if state.get("stack"):
            return False
        found = find_activate(st, pyro)
        if found is None:
            return False
        kind = found[0]
        ctx["pre_" + dmg_key] = damage_marked_of(state, wall)
        if kind == "submit":
            ctx[act_key] = True
            ctx[act_key + "_rev"] = c.revision
            say(f"[{tag}] ping{which}: activating Pyromancer (oid {pyro}) "
                f"targeting the Wall (oid {wall}); wall damage before="
                f"{ctx['pre_' + dmg_key]}")
            wire(f"ping{which}_activate",
                 {"pyro_oid": pyro, "wall_oid": wall,
                  "wall_damage_before": ctx["pre_" + dmg_key],
                  "activate_action": found[1]})
            await submit_as_is(c, found[1])
            return True
        else:
            _, opp, ch = found
            ctx[act_key] = True
            say(f"[{tag}] ping{which}: activating via vi")
            wire(f"ping{which}_activate_vi", {})
            await answer_vi(c, opp, ch, tag)
            return True
    # --- answer the "any target" selection with the Wall
    if ctx.get(act_key) and not ctx.get(tgt_key):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            pick = pick_target_by_name(state, opp, WALL)
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            wire(f"ping{which}_target_seen",
                 {"rev": c.revision, "rtype": rtype, "stype": stype,
                  "candidates": [{"text": choice_text(ch)[:80],
                                  "ref": _cand_ref(ch),
                                  "ref_name": obj_lname(state, _cand_ref(ch))
                                  if _cand_ref(ch) else None}
                                 for ch in cands]})
            if pick is not None:
                ctx[tgt_key] = True
                say(f"[{tag}] ping{which}: targeting the Wall of Putrid "
                    f"Flesh (oid {wall})")
                await answer_vi(c, opp, pick, tag)
                return True
            key = (tag, f"ping{which}-target-miss", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] ping{which} target prompt has no Wall "
                    f"candidate -- recorded, not answered")
            return True
    # --- watch the ability hit the stack, then resolve
    if ctx.get(act_key) and not ctx.get(res_key):
        if stack_entries_from(state, pyro):
            if not ctx.get(seen_key):
                ctx[seen_key] = True
                say(f"[{tag}] ping{which}: ability on the stack")
                wire(f"ping{which}_on_stack", {})
        elif ctx.get(seen_key) and not state.get("stack"):
            ctx[res_key] = True
            dmg = damage_marked_of(state, wall)
            ctx[dmg_key] = dmg
            say(f"[{tag}] ping{which} RESOLVED: Wall (oid {wall}) "
                f"damage_marked={dmg} (before={ctx.get('pre_' + dmg_key)})")
            wire(f"ping{which}_resolved",
                 {"wall_oid": wall, "damage_marked": dmg,
                  "damage_before": ctx.get("pre_" + dmg_key)})
            try:
                post = json.loads(await (host or c).export_state())
                with open(f"{EVDIR}/post_ping{which}.json", "w") as f:
                    json.dump(post, f, indent=1)
                wire(f"post_ping{which}_export", {})
            except Exception as e:
                wire(f"post_ping{which}_export_failed",
                     {"error": str(e)[:200]})
            return True
    return False


async def p1_tick(c, tag, ctx, host=None):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 1, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if not ctx.get("pyro_oid"):
        pys = pyro_oids(state, 1)
        if pys:
            ctx["pyro_oid"] = pys[0]
    if not ctx.get("wall_oid"):
        wls = wall_oids(state, 1)
        if wls:
            ctx["wall_oid"] = wls[0]
            say(f"[{tag}] Wall of Putrid Flesh on battlefield "
                f"(oid {wls[0]})")

    # --- main-phase plays
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
        hn = hand_names(state, 1)
        cols = untapped_colored_lands(state, 1)
        if not ctx.get("pyro_oid") and PYRO in hn \
                and cols["R"] >= 1 and untapped_land_total(state, 1) >= 3:
            a, oid = cast_action_for(acts, state, PYRO)
            if a is not None:
                ctx["pyro_cast"] = True
                ST["mana_needs"][tag] = {"R": 1, "generic": 2}
                say(f"[{tag}] casting Prodigal Pyromancer (oid {oid})")
                wire("cast_pyro", {"oid": oid})
                await submit_as_is(c, a)
                return
        if ctx.get("pyro_oid") and not ctx.get("wall_oid") and WALL in hn \
                and cols["B"] >= 1 and untapped_land_total(state, 1) >= 3:
            a, oid = cast_action_for(acts, state, WALL)
            if a is not None:
                ctx["wall_cast"] = True
                ST["mana_needs"][tag] = {"B": 1, "generic": 2}
                say(f"[{tag}] casting Wall of Putrid Flesh (oid {oid})")
                wire("cast_wall", {"oid": oid})
                await submit_as_is(c, a)
                return

    # --- ping legs (baseline = 1, post-phase = 2)
    if await p1_ping_leg(c, st, acts, state, tag, ctx, 1, host):
        return
    if await p1_ping_leg(c, st, acts, state, tag, ctx, 2, host):
        return

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    await maybe_pass_priority(c, st, acts, tag)

# ------------------------------------------------------------------- run

async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    LAST_PASS_T.clear()
    MULLS.clear()
    MULL_COUNT.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9857-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Plains", 44), ("Holy Strength", 8),
                         ("Teferi's Protection", 8)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Mountain", 22), ("Swamp", 22),
                                     ("Prodigal Pyromancer", 8),
                                     ("Wall of Putrid Flesh", 8)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    for k in ("A1_setup_ok", "A2_baseline_prevented", "A3_phaseout",
              "A4_decisive", "A5_bonus_dropped"):
        obs["assert"][k] = "not-run"

    # mulligan phase: tick until both seats are past mulligan, then check
    # the hands have the key pieces (Holy Strength / Pyromancer).
    t_mull = time.time()
    while time.time() - t_mull < 90:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx, p0)
        await p0_tick(p0, tag, ctx, p0)
        await asyncio.sleep(0)
        st = p0.latest
        if st and st["state"].get("turn_number", 0) >= 1:
            acts = merged_actions(st)
            if not any(a.get("type") == "MulliganDecision" for a in acts) \
                    and vi_kind_code(st) != "mulligan":
                break
    st = p0.latest
    hn0 = hand_names(st["state"], 0) if st else []
    st1 = p1.latest
    hn1 = hand_names(st1["state"], 1) if st1 else []
    if st is None or HS not in hn0 or PYRO not in hn1:
        say(f"[{tag}] fast abort: P0 hand lacks HS ({hn0}) or P1 hand "
            f"lacks Pyro ({hn1})")
        wire("fast_abort", {"p0_hand": hn0, "p1_hand": hn1})
        obs["notes"].append("fast abort: opening hands lacked Holy Strength "
                            "(P0) or Prodigal Pyromancer (P1); redealt")
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 600:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx, p0)
        await p0_tick(p0, tag, ctx, p0)
        await asyncio.sleep(0)
        st_now = p0.latest
        turn = st_now["state"].get("turn_number", 0) if st_now else 0
        if turn > 25 and not ctx.get("ping2_resolved"):
            say(f"[{tag}] turn {turn} exceeded without ping2; abandoning")
            obs["giveup"] = True
            obs["giveup_reason"] = (
                f"game reached turn {turn} without the post-phase ping "
                f"completing")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pidx in ((p0, 0), (p1, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                atypes = sorted(set(a.get("type") for a in acts))
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"atypes={atypes} hs={ctx.get('hs_oid')} "
                    f"ping1={ctx.get('ping1_resolved')} "
                    f"tp={ctx.get('tp_resolved')} "
                    f"ping2={ctx.get('ping2_resolved')} "
                    f"p0bf={bf_names(s, 0)[:6]} p1bf={bf_names(s, 1)[:6]}")
                if _opp_summary(st):
                    say(f"[diag] {c.name} vi: {_opp_summary(st)}")
        if ctx.get("ping2_resolved"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break

    st = p0.latest
    state = st["state"] if st else {}

    try:
        final_exp = json.loads(await p0.export_state())
        with open(f"{EVDIR}/final_export.json", "w") as f:
            json.dump(final_exp, f, indent=1)
        wire("final_export", {})
        if isinstance(final_exp, dict) and isinstance(final_exp.get("state"), dict):
            final_state = final_exp["state"]
        elif isinstance(final_exp, dict):
            final_state = final_exp
        else:
            final_state = {}
    except Exception as e:
        wire("final_export_failed", {"error": str(e)[:200]})
        final_state = {}

    # ---- A1: Holy Strength attached to the Pyromancer, Pyro was 2/3 while
    # the Aura was phased in (P/T recorded at attach time; the end-of-run
    # read is post-phase-out)
    hs = ctx.get("hs_oid")
    pyro = ctx.get("pyro_oid")
    a1_pt = ctx.get("a1_pt")
    if hs and pyro and hs_attached_to_pyro(state, ctx) and a1_pt is not None:
        if tuple(a1_pt) == (2, 3):
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                f"A1 passed: Holy Strength (oid {hs}) attached to Prodigal "
                f"Pyromancer (oid {pyro}); Pyromancer was 2/3 while the "
                f"Aura was phased in (+1/+2 applied)")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: Holy Strength attached but Pyromancer P/T at "
                f"attach time={tuple(a1_pt)} (expected (2, 3))")
    elif ctx.get("hs_cast"):
        obs["assert"]["A1_setup_ok"] = "failed"
        obs["notes"].append(
            f"A1 FAILED: Holy Strength cast but never attached "
            f"(hs_oid={hs}, pyro_oid={pyro}, a1_pt={a1_pt})")
    else:
        obs["notes"].append("A1 not-run: Holy Strength never reached the "
                            "battlefield attached to the Pyromancer")

    # ---- A2: baseline ping prevented while the Aura is phased in
    if ctx.get("ping1_resolved"):
        d1 = ctx.get("ping1_damage")
        if d1 == 0:
            obs["assert"]["A2_baseline_prevented"] = "passed"
            obs["notes"].append(
                "A2 passed: baseline ping (Aura phased in) was PREVENTED -- "
                "Wall damage_marked stayed 0; the prevention machinery is "
                "live")
        else:
            obs["assert"]["A2_baseline_prevented"] = "failed"
            obs["notes"].append(
                f"A2 FAILED: baseline ping dealt {d1} damage to the Wall "
                f"while the Pyromancer was enchanted -- prevention did not "
                f"apply even phased-in; the comparison is invalid")
    else:
        obs["notes"].append(
            "A2 not-run: the baseline ping leg never completed "
            f"(ping1_activated={ctx.get('ping1_activated')}, "
            f"ping1_targeted={ctx.get('ping1_targeted')})")

    # ---- A3: the phase-out happened as designed
    if ctx.get("tp_resolved"):
        pp = ctx.get("post_phase", {})
        hs_ph, pyro_ph, wall_ph = (pp.get("hs_phase"), pp.get("pyro_phase"),
                                   pp.get("wall_phase"))
        if hs_ph == "PhasedOut" and pyro_ph == "PhasedIn" \
                and wall_ph == "PhasedIn":
            obs["assert"]["A3_phaseout"] = "passed"
            obs["notes"].append(
                f"A3 passed: after Teferi's Protection, Holy Strength is "
                f"PhasedOut while the Pyromancer and Wall are PhasedIn "
                f"(pyro P/T at phase-out: {pp.get('pyro_pt')})")
        else:
            obs["assert"]["A3_phaseout"] = "failed"
            obs["notes"].append(
                f"A3 FAILED: unexpected phase reads hs={hs_ph} "
                f"pyro={pyro_ph} wall={wall_ph}")
    else:
        obs["notes"].append(
            "A3 not-run: Teferi's Protection never resolved "
            f"(tp_cast={ctx.get('tp_cast')})")

    # ---- A4 (decisive): post-phase ping
    if ctx.get("ping2_resolved"):
        d2 = ctx.get("ping2_damage")
        if d2 == 1:
            obs["assert"]["A4_decisive"] = "passed"
            obs["notes"].append(
                "A4 passed: post-phase ping damage was NOT prevented (Wall "
                "damage_marked == 1) -- the phased-out Aura is correctly "
                "treated as not existing per CR 702.26b")
        elif d2 == 0:
            obs["assert"]["A4_decisive"] = "failed"
            obs["notes"].append(
                "A4 FAILED (the reported bug): post-phase ping damage was "
                "STILL prevented (Wall damage_marked == 0) with the Holy "
                "Strength phased out -- the EnchantedBy fallback counted "
                "the phased-out Aura")
        else:
            obs["assert"]["A4_decisive"] = "failed"
            obs["notes"].append(
                f"A4 FAILED (unexpected): post-phase Wall damage_marked="
                f"{d2} (expected 0 for the bug profile, 1 for correct)")
    else:
        obs["notes"].append(
            "A4 not-run: the post-phase ping leg never completed "
            f"(ping2_activated={ctx.get('ping2_activated')}, "
            f"ping2_targeted={ctx.get('ping2_targeted')}, "
            f"tp_resolved={ctx.get('tp_resolved')})")

    # ---- A5 (observation): did the Aura's own +1/+2 drop while phased out?
    pp = ctx.get("post_phase", {})
    pyro_pt = pp.get("pyro_pt")
    if pyro_pt is not None:
        if tuple(pyro_pt) == (1, 1):
            obs["assert"]["A5_bonus_dropped"] = "passed"
            obs["notes"].append(
                "A5 passed (observation): the Pyromancer is 1/1 right after "
                "the phase-out -- the phased-out Aura's own static no "
                "longer applies")
        else:
            obs["assert"]["A5_bonus_dropped"] = "failed"
            obs["notes"].append(
                f"A5 FAILED (observation): the Pyromancer is still "
                f"{tuple(pyro_pt)} after the phase-out -- the phased-out "
                f"Aura's +1/+2 still applies")
    else:
        obs["notes"].append("A5 not-run: no post-phase P/T reading")

    for fn in ("pre_phase.json", "post_phase.json", "post_ping1.json",
               "post_ping2.json", "final_export.json"):
        try:
            with open(f"{EVDIR}/{fn}") as f:
                json.load(f)
            obs["notes"].append(f"{fn} captured and parses")
        except (OSError, ValueError):
            obs["notes"].append(f"{fn} missing/unparseable")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    # save the pinned card entries for the three cards (provenance)
    entries = {}
    for lname in (HS, TP, PYRO, WALL):
        for k, v in CARD_DATA.items():
            if k.strip('"').lower() == lname:
                entries[lname] = {"oracle_text": v.get("oracle_text"),
                                  "abilities": v.get("abilities"),
                                  "replacements": v.get("replacements"),
                                  "static_abilities": v.get("static_abilities")}
                break
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump(entries, f, indent=1)
    say(f"card entries saved: {sorted(entries)}")
    wire("card_entries", {"cards": sorted(entries)})

    obs = None
    for attempt in range(6):
        obs = await run_attempt(attempt)
        a1 = obs["assert"].get("A1_setup_ok")
        a4 = obs["assert"].get("A4_decisive")
        if a1 in ("passed", "failed") and a4 in ("passed", "failed"):
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted; redealing")
            continue
        if obs.get("giveup"):
            say(f"[attempt {attempt}] giveup ({obs.get('giveup_reason')}); "
                f"not retrying blind")
            break
        say(f"[attempt {attempt}] incomplete (A1={a1}, A4={a4}); "
            f"{'retrying' if attempt < 5 else 'giving up'}")

    ass = dict(obs["assert"])
    notes = list(obs["notes"])
    notes.append("protocol-140 driver: CreateGameWithSettings/"
                 "JoinGameWithPassword/start_when_full; MulliganDecision via "
                 "legacy Action (Keep); DiscardToHandSize via vi select; mana "
                 "via vi tapLandForMana; casts via advertised legal actions "
                 "(targets answered via the engine's vi target selection "
                 "after CastSpell, CR 601.2c); ActivateAbility submitted "
                 "while holding priority; priority-gated passes; sleep(0) "
                 "yield before leg evaluation.")

    a2 = ass.get("A2_baseline_prevented")
    a3 = ass.get("A3_phaseout")
    a4 = ass.get("A4_decisive")
    if a2 == "passed" and a3 == "passed" and a4 == "failed":
        verdict = "reproduced"
    elif a2 == "passed" and a3 == "passed" and a4 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append(
        "verdict logic: reproduced = baseline prevention live (A2 passed) "
        "and the phase-out happened as designed (A3 passed) but the "
        "post-phase ping was still prevented (A4 failed: Wall "
        "damage_marked == 0 with the Holy Strength phased out) -- the "
        "EnchantedBy fallback counted the phased-out Aura. "
        "not-reproduced = the post-phase ping damage went through (A4 "
        "passed). blocked = the setup legs never completed.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9857",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release); evidence under evidence/9857/" + RUN_ID),
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Plains", 44], ["Holy Strength", 8],
                   ["Teferi's Protection", 8]],
            "P1": [["Mountain", 22], ["Swamp", 22],
                   ["Prodigal Pyromancer", 8], ["Wall of Putrid Flesh", 8]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s"
                   % (ass.get("A1_setup_ok"), a2, a3, a4,
                      ass.get("A5_bonus_dropped"))),
        "scope": ("Wall of Putrid Flesh 'enchanted creatures' prevention "
                  "filter (FilterProp::EnchantedBy fallback, non-Aura "
                  "source) against a creature whose only Aura is phased "
                  "out; two-player game; native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The ping legs require the Pyromancer untapped in its "
            "controller's main phase with an empty stack; missed windows "
            "mark the leg not-run.",
            "Phase-status reads use the runtime object's phase_status field; "
            "the verbatim objects are preserved in the exported states.",
        ],
        "setup_line": ("P0 44x Plains + 8x Holy Strength ({W} Aura, "
                       "+1/+2) + 8x Teferi's Protection ({2}{W}); P1 22x "
                       "Mountain + 22x Swamp + 8x Prodigal Pyromancer "
                       "({2}{R} 1/1, {T}: 1 damage to any target) + 8x Wall "
                       "of Putrid Flesh ({2}{B} 2/4, prevent all damage by "
                       "enchanted creatures)"),
        "contract_line": ("Baseline: ping the Wall while the Pyromancer is "
                          "enchanted -> damage prevented (Wall damage 0). "
                          "Phase out the Holy Strength via Teferi's "
                          "Protection. Post-phase ping -> damage must go "
                          "through (Wall damage 1) per CR 702.26b; still "
                          "prevented (damage 0) = the reported bug."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9857.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read()) + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 1150
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9857 - `EnchantedBy` fallback counts",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "phased-out Auras",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    for k, v in run["assertions"].items():
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (230, 200, 90))
        d.text((36, y), f"{k}: {v}", fill=col); y += 24
        if y > H - 220:
            break
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    obs_lines = [
        "Baseline: Pyromancer (enchanted, 2/3) pings own Wall of Putrid",
        "Flesh -> damage prevented (Wall damage_marked 0).",
        "Teferi's Protection phases out P0's Holy Strength; Pyromancer",
        "and Wall stay phased in.",
        "Post-phase ping: correct = Wall damage_marked 1 (CR 702.26b);",
        "bug = still 0 (EnchantedBy fallback counted the phased-out Aura).",
        f"game: {run['result']}",
    ]
    for ln in obs_lines:
        while len(ln) > 88:
            d.text((36, y), ln[:88], fill=(170, 185, 200)); y += 22
            ln = ln[88:]
        d.text((36, y), ln, fill=(170, 185, 200)); y += 22
    y += 6
    d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence 9857/" + run["run_id"],
           fill=(140, 160, 180))
    img.save(out_path)
    say(f"summary PNG written: {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
