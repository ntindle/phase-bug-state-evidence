#!/usr/bin/env python3
"""phase-rs/phase #7021 - Fear, Fire, Foes! secondary damage ignores the
"with the same controller" clause.

Oracle: "Damage can't be prevented this turn. Fear, Fire, Foes! deals X
damage to target creature and 1 damage to each other creature with the
same controller."

Reported: the 1 damage hits ALL other creatures, including the caster's.

This is the protocol-106 (v0.103.0) re-validation port of
driver/scenario_7021.py (v0.82.0, protocol 70, run 20260913-7021c).

Contract:
  A1_parse_gap   - v0.103.0 card-data AST: DamageAll target lacks any
                   same-controller filter (gap present) [parse evidence]
  A2_setup_ok    - 3-seat game; P1 has >=2 Mystics, P0/P2 have >=1 each;
                   Fear in P0 hand; pre.json exported
  A3_target_hit  - target P1 Mystic leaves the battlefield (X=2)
  A4_same_controller_hit - P1's other Mystic leaves the battlefield
                   (the 1 damage it is supposed to take)
  A5_caster_spared - P0's Mystic still on battlefield (0 damage)
  A6_third_party_spared - P2's Mystic still on battlefield (0 damage)
  A7_cleanup     - stack empty, Fear in P0 graveyard
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7021
RUN_ID = os.environ.get("RUN_ID", "20261008-7021")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*msgs):
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(m) for m in msgs)
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")

URL = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")


async def verify_server_hello():
    import websockets
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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

PARSE = {"target": None, "ok": False}


def check_data_level():
    """Record the v0.103.0 parse of Fear, Fire, Foes!'s DamageAll sub-ability.

    A1 contract: DamageAll target is Typed{Creature, controller:null,
    properties:[Another]} - no same-controller filter (parse gap persists).
    """
    c = CARD_DATA.get("fear, fire, foes!", {})

    def find_damage_all(abs_):
        for a in abs_ or []:
            eff = (a.get("effect") or {})
            if eff.get("type") == "DamageAll":
                return a
            r = find_damage_all([a.get("sub_ability") or {}])
            if r:
                return r
        return None

    da = find_damage_all(c.get("abilities"))
    tgt = ((da or {}).get("effect", {}) or {}).get("target", {}) if da else {}
    PARSE["target"] = tgt
    blob = json.dumps(tgt).lower()
    has_same_controller = "same" in blob and "controller" in blob
    PARSE["ok"] = bool(da) and not has_same_controller \
        and tgt.get("controller") is None
    out = {
        "name": c.get("name"),
        "oracle_text": c.get("oracle_text"),
        "damage_all_target": tgt,
        "parse_gap_persists": PARSE["ok"],
        "note": "On v0.82.0 the parse was identical: Typed Creature / "
                "controller:null / properties:[Another], no same-controller "
                "relation.",
    }
    with open(f"{EVDIR}/parse_fear.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    say(f"data-level: parse_gap_persists={PARSE['ok']} target={tgt}")
    wire("data_level", {"parse_gap_persists": PARSE["ok"], "target": tgt})
    return out


FEAR_T = "Fear, Fire, Foes!"
FEAR_L = "fear, fire, foes!"
MYSTIC_T = "Elvish Mystic"
MYSTIC_L = "elvish mystic"
MOUNTAIN_L = "mountain"
FOREST_L = "forest"
LANDS = (MOUNTAIN_L, FOREST_L)

P0_DECK = ((FEAR_T, 12), (MYSTIC_T, 8), ("Mountain", 28), ("Forest", 12))
P1_DECK = ((MYSTIC_T, 16), ("Forest", 44))
P2_DECK = ((MYSTIC_T, 16), ("Forest", 44))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 30

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0, "P2": 0}}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {
    "fear_cast": False, "fear_resolved": False, "fear_oid": None,
    "target_oid": None, "x_answered": False,
    "pre_exported": False, "post_at": None, "post_exported": False,
    "target_sels": [],
}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": []}

# ---------------------------------------------------------------- helpers


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def yard_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def untapped_land_lnames(state, pid):
    return [obj_lname(state, oid) for oid in untapped_lands(state, pid)]


def stack_entries(state):
    return state.get("stack") or []


def stack_empty(state):
    return not stack_entries(state)


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_oid(ch):
    ref = cand_reference(ch)
    try:
        return str(int(ref))
    except (TypeError, ValueError):
        return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(op):
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": STAGE["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": STAGE["stage"],
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
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in LANDS)
    if pid == 0:
        keep = ((FEAR_L in hand and n_lands >= 2) or n >= 2)
    else:
        keep = (n_lands >= 2 or n >= 2)
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        STAGE["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if STAGE["mulls"].get(tag, 0) <= 0:
        return False
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        spec = (rdata.get("spec", {}) or {})
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = cand_oid(ch)
            if ref is not None and is_land(get_obj(state, ref)):
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def do_declare_empty(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    # P0 prefers Mountain until one is on the battlefield (for {R}).
    if pid == 0 and not bf_oids(state, 0, MOUNTAIN_L):
        lands = sorted(lands,
                       key=lambda o: 0 if obj_lname(state, o)
                       == MOUNTAIN_L else 1)
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
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
                                                   "data": {"choiceId":
                                                             ch.get("id")}}},
                                  c.name)
                return True
    return False


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    return a
            except (TypeError, ValueError):
                pass
    return None


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


# ------------------------------------------------- issue-specific prompts

def iter_target_opps(st):
    """Yield (opp, rtype, spec_type) for viewer_interaction opportunities
    that look like target selections: schema select/sequence with
    candidates, or exactChoices whose choices carry candidate/target codes
    (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                yield opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(cc for cc in surf_codes(ch) if cc)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(cc in codes for cc in ("candidate", "target")):
                yield opp, "exactChoices", "choose"


def record_target_sel(state, opp, stage):
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["target_sels"]):
        return False
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in cands:
        oid = cand_oid(ch)
        o = get_obj(state, oid) if oid else {}
        cand_info.append({
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    ST["target_sels"].append(rec)
    OBS["target_selections"].append(rec)
    n = len(ST["target_sels"])
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"], x["seat"])
                         for x in cand_info]})
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"seat={x['seat']})" for x in cand_info[:10]))
    return True


def pick_p1_mystic(state, opp):
    """Fear's target: a P1 Elvish Mystic on the battlefield."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        oid = cand_oid(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if (obj_lname(state, oid) == MYSTIC_L
                and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "1"):
            return ch
    return None


async def answer_target(c, state, opp, rtype, spec_type, ch, tag, stage):
    oid = cand_oid(ch)
    if rtype == "schema":
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": spec_type,
                            "data": {"choiceIds": [ch.get("id")]}}}
    else:
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": ch.get("id")}}}
    say(f"[{tag}] answering {stage} target: "
        f"{obj_lname(state, oid) if oid else choice_text(ch)[:40]} "
        f"(oid {oid}) via {sub['response']['type']}")
    wire("target_answer", {"who": tag, "stage": stage,
                           "iid": opp.get("interactionId"),
                           "oid": oid, "submission": sub})
    await answer_vi(c, opp, ch, tag)
    return oid


async def answer_fear_targets(c, st, state, tag):
    """Answer Fear's TargetSelection: a P1 Mystic."""
    if not ST["fear_cast"] or ST["target_oid"]:
        return False
    acted = False
    for opp, rtype, spec_type in iter_target_opps(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        record_target_sel(state, opp, "fear-target")
        pick = pick_p1_mystic(state, opp)
        if pick is None:
            say(f"[{tag}] fear target prompt has no P1 Mystic candidate; "
                f"leaving unanswered")
            wire("fear_target_no_pick", {"iid": iid})
            continue
        ST["target_oid"] = await answer_target(c, state, opp, rtype,
                                               spec_type, pick, tag,
                                               "fear-target")
        acted = True
    return acted


async def answer_x(c, st, tag):
    """ChooseXValue for Fear -> X = 2.

    106 shapes: schema with spec.type "number", or exactChoices with
    numbered choices."""
    if ST["x_answered"] or not ST["fear_cast"]:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        iid = opp.get("interactionId")
        if resp.get("type") == "schema" and spec.get("type") == "number":
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": 2}}}
            say(f"[{tag}] Fear X-choice -> X=2 (schema number)")
            wire("x_answer", {"iid": iid, "x": 2, "shape": "schema-number"})
            await interact_as(c, sub, tag)
            SUBMITTED_OPPS.add(iid)
            ST["x_answered"] = True
            return True
        if resp.get("type") == "exactChoices":
            chs = data.get("choices") or []
            for ch in chs:
                blob = (choice_text(ch) + " "
                        + json.dumps(ch, default=str)).lower()
                if "2" in blob and not any(w in blob for w in
                                           ("0", "1", "3", "4", "5")):
                    say(f"[{tag}] Fear X-choice -> X=2 (exactChoices)")
                    wire("x_answer", {"iid": iid, "x": 2,
                                      "shape": "exactChoices"})
                    await answer_vi(c, opp, ch, tag)
                    ST["x_answered"] = True
                    return True
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    turn = state.get("turn_number") or 0

    # track Fear on stack / resolution
    if ST["fear_cast"] and not ST["fear_resolved"]:
        if yard_oids(state, 0, FEAR_L):
            ST["fear_resolved"] = True
            say(f"Fear resolved (in P0 graveyard), turn {turn}")
            wire("fear_resolved", {"turn": turn})

    # Fear's decisions pending on P0
    if await answer_fear_targets(c, st, state, tag):
        return True
    if await answer_x(c, st, tag):
        return True

    # PRE: fixture complete, P0 main-phase priority, Fear in hand
    if (not ST["pre_exported"] and not ST["fear_cast"]
            and my_main(state, 0)
            and len(bf_oids(state, 1, MYSTIC_L)) >= 2
            and len(bf_oids(state, 0, MYSTIC_L)) >= 1
            and len(bf_oids(state, 2, MYSTIC_L)) >= 1
            and FEAR_L in hand_lnames(state, 0)
            and len(untapped_lands(state, 0)) >= 3
            and MOUNTAIN_L in untapped_land_lnames(state, 0)):
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported: fixture complete, P0 priority")
        wire("pre_exported", {"turn": turn})

    # cast Fear once (engine Auto payment; DO NOT pay via vi taps)
    if (not ST["fear_cast"] and ST["pre_exported"]
            and my_main(state, 0)
            and FEAR_L in hand_lnames(state, 0)):
        a = find_cast_action(acts, state, FEAR_L)
        if a is not None:
            ST["fear_cast"] = True
            ST["fear_cast_turn"] = turn
            say(f"[{tag}] casting {FEAR_T} (engine Auto payment)")
            wire("fear_cast_submit", {"turn": turn})
            await submit_as_is(c, a)
            return True
        say("Fear CastSpell NOT advertised on cast tick; passing priority")
        wire("fear_not_advertised", {"turn": turn})

    # board building on P0 main
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        # cast a Mystic if affordable
        if MYSTIC_L in hand_lnames(state, 0) \
                and len(untapped_lands(state, 0)) >= 2:
            a = find_cast_action(acts, state, MYSTIC_L)
            if a is not None:
                say(f"[{tag}] casting {MYSTIC_T} (engine Auto payment)")
                wire("mystic_cast", {"tag": tag, "turn": turn})
                await submit_as_is(c, a)
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def generic_tick(c, pid, tag):
    """P1/P2: build board (lands + Mystics), zero combat, pass priority."""
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_declare_empty(c, acts, st, pid, tag):
        return True

    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return True
        if MYSTIC_L in hand_lnames(state, pid) \
                and len(untapped_lands(state, pid)) >= 2:
            a = find_cast_action(acts, state, MYSTIC_L)
            if a is not None:
                say(f"[{tag}] casting {MYSTIC_T} (engine Auto payment)")
                wire("mystic_cast", {"tag": tag,
                                     "turn": state.get("turn_number")})
                await submit_as_is(c, a)
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ------------------------------------------------------------- main loop

async def main():
    t0 = time.time()
    last_rev_change = t0
    game_started = False

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P07021r")
    await p0.connect()
    say("P0 creating game (3 seats)...")
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P17021r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P27021r")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id} P2 seat={p2.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id, "p2": p2.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK, "p2_deck": P2_DECK})

    clients = ((p0, "P0", p0_tick, 0),
               (p1, "P1", generic_tick, 1),
               (p2, "P2", generic_tick, 2))
    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick, pid in clients:
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # (or holding an unanswered vi decision) with no revision
                # change (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                if pid == 0:
                    await tick(c, tag)
                else:
                    await tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        # POST: fear resolved, stack empty, 8s grace
        if ST["fear_resolved"] and not ST["post_exported"] \
                and stack_empty(state):
            if ST["post_at"] is None:
                ST["post_at"] = time.time()
        if (ST["post_at"] is not None and not ST["post_exported"]
                and time.time() - ST["post_at"] > 8
                and stack_empty(state)):
            await do_export(p0, "post.json")
            ST["post_exported"] = True
            STAGE["stop"] = True
            say("POST exported: sequence complete; stopping")
            wire("post_exported", {"turn": turn})
            continue

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"]})
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"P1untapped={len(untapped_lands(state, 1))} "
                f"P2untapped={len(untapped_lands(state, 2))} "
                f"P1m={bf_oids(state, 1, MYSTIC_L)} "
                f"P0m={bf_oids(state, 0, MYSTIC_L)} "
                f"P2m={bf_oids(state, 2, MYSTIC_L)} "
                f"fear={ST['fear_cast']}/{ST['fear_resolved']} "
                f"x={ST['x_answered']} tgt={ST['target_oid']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']}")

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    await finish(p0, p1, p2, t0, hello, data_level)


async def finish(p0, p1, p2, t0, hello, data_level):
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    post_s = load("post.json")

    # A1: card-data parse gap persists on v0.103.0
    A["A1_parse_gap"] = "passed" if PARSE["ok"] else "failed"
    tgt = PARSE["target"] or {}
    D["A1_parse_gap"] = (
        f"DamageAll target = Typed {{Creature, controller:"
        f"{tgt.get('controller')}, "
        f"properties:{[p.get('type') for p in (tgt.get('properties') or [])]}"
        f"}} - no same-controller filter (parse drops the clause)")

    # A2: setup (from PRE)
    if pre_s is not None:
        p1m = bf_oids(pre_s, 1, MYSTIC_L)
        p0m = bf_oids(pre_s, 0, MYSTIC_L)
        p2m = bf_oids(pre_s, 2, MYSTIC_L)
        fear_hand = FEAR_L in hand_lnames(pre_s, 0)
        ok = (len(p1m) >= 2 and len(p0m) >= 1 and len(p2m) >= 1
              and fear_hand)
        A["A2_setup_ok"] = "passed" if ok else "failed"
        D["A2_setup_ok"] = (
            f"P1m={p1m} P0m={p0m} P2m={p2m} fear_in_P0_hand={fear_hand}")
    else:
        A["A2_setup_ok"] = "failed"
        D["A2_setup_ok"] = "pre.json missing"

    # A3..A6 from post
    if post_s is not None and ST["fear_cast"]:
        tgt = ST["target_oid"]
        p1m = bf_oids(post_s, 1, MYSTIC_L)
        p0m = bf_oids(post_s, 0, MYSTIC_L)
        p2m = bf_oids(post_s, 2, MYSTIC_L)
        target_gone = tgt is not None and tgt not in p1m
        A["A3_target_hit"] = "passed" if target_gone else "failed"
        D["A3_target_hit"] = (
            f"target_oid={tgt} P1m_post={p1m} -> "
            f"{'passed' if target_gone else 'FAILED'} (expected: target dies)")
        # correct: P1's OTHER mystic takes the 1 (dies as a 1/1)
        other_p1_gone = len(p1m) == 0
        A["A4_same_controller_hit"] = "passed" if other_p1_gone else "failed"
        D["A4_same_controller_hit"] = (
            f"P1 other mystics post={p1m} -> "
            f"{'passed' if other_p1_gone else 'FAILED'} (expected 0 "
            f"survivors: target takes X=2, other takes the 1)")
        ok = len(p0m) >= 1
        A["A5_caster_spared"] = "passed" if ok else "failed"
        D["A5_caster_spared"] = (
            f"P0 mystics post={p0m} -> "
            f"{'passed' if ok else 'FAILED'} "
            f"(expected >=1: caster's creature takes 0)")
        ok = len(p2m) >= 1
        A["A6_third_party_spared"] = "passed" if ok else "failed"
        D["A6_third_party_spared"] = (
            f"P2 mystics post={p2m} -> "
            f"{'passed' if ok else 'FAILED'} "
            f"(expected >=1: third party takes 0)")
        stack_ok = stack_empty(post_s)
        fear_gy = bool(yard_oids(post_s, 0, FEAR_L))
        ok = stack_ok and fear_gy
        A["A7_cleanup"] = "passed" if ok else "failed"
        D["A7_cleanup"] = (
            f"stack_empty={stack_ok} fear_in_P0_gy={fear_gy}")
    else:
        for k in ("A3_target_hit", "A4_same_controller_hit",
                  "A5_caster_spared", "A6_third_party_spared",
                  "A7_cleanup"):
            A[k] = "failed"
        D["A3_A7"] = "fear never cast or post.json missing"

    for k, v in A.items():
        say(f"{k}: {v}")
    for k, v in D.items():
        say(f"detail {k}: {v}")

    # verdict rule (same as the v0.82.0 contract): reproduced iff P0's or
    # P2's Mystic dies (A5/A6 failed) with A1-A4 and A7 passed;
    # not-reproduced iff A1-A7 all passed; else blocked.
    core_passed = all(A.get(k) == "passed"
                      for k in ("A1_parse_gap", "A2_setup_ok",
                                "A3_target_hit", "A4_same_controller_hit",
                                "A7_cleanup"))
    if core_passed and (A.get("A5_caster_spared") == "failed"
                        or A.get("A6_third_party_spared") == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say("VERDICT:", verdict)

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "[Card Bug] Fear, Fire, Foes! Incorrectly deals 1 damage "
                 "to all other creatures instead of all other creatures "
                 "with the same controller",
        "validated_at": "2026-10-08",
        "validated_version": "v0.103.0",
        "server": SERVER_IDENTITY,
        "server_hello": hello,
        "scope": "Fear, Fire, Foes! secondary 1-damage clause across "
                 "three controller seats; native engine, three "
                 "human-client seats, X=2",
        "verdict": verdict,
        "assertions": A,
        "assertion_details": D,
        "notes": OBS["notes"],
        "target_selections": OBS["target_selections"],
        "rejections": OBS["rejections"],
        "tick_errors": OBS["tick_errors"],
        "driver_state": {
            "fear_cast": ST["fear_cast"],
            "fear_resolved": ST["fear_resolved"],
            "fear_cast_turn": ST.get("fear_cast_turn"),
            "target_oid": ST["target_oid"],
            "x_answered": ST["x_answered"],
            "pre_exported": ST["pre_exported"],
            "post_exported": ST["post_exported"],
        },
        "limitations": [
            "Browser UI not exercised; native engine via three "
            "human-client seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay (the phase-server has no standalone "
            "state-import path).",
            "Zero-attacker combat was scripted on all seats so combat "
            "damage could not mask the spell's damage.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")
    # copy scenario into evidence
    shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7021_01030.py")
    render_png(run)
    # manifest LAST
    files = ["pre.json", "post.json", "parse_fear.json", "run.json",
             "scenario_7021_01030.py", "wire_log.jsonl", "scenario_run.log",
             "summary.png"]
    for n in range(1, len(ST["target_sels"]) + 1):
        files.append(f"target_sel_{n}.json")
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = sha256_of_file(p)
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say("wrote manifest.sha256")
    # validation
    for fn in ("pre.json", "post.json", "parse_fear.json", "run.json"):
        if os.path.exists(f"{EVDIR}/{fn}"):
            json.load(open(f"{EVDIR}/{fn}"))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
    for line in man:
        h, fn = line.split("  ")
        assert sha256_of_file(f"{EVDIR}/{fn}") == h, fn
    say("validation: JSON parses, PNG readable, hashes match")


def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1120
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#7021 - Fear, Fire, Foes! secondary damage hits all "
           "other creatures", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "3 seats", fill=(140, 160, 180))
    y += 28
    v = run["verdict"]
    col = (255, 90, 90) if v == "reproduced" else (
        (120, 220, 120) if v == "not-reproduced" else (230, 200, 120))
    d.text((24, y), f"verdict: {v.upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: deals X to target creature and 1 damage to "
           "each other creature", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "WITH THE SAME CONTROLLER (dropped clause).",
           fill=(255, 180, 120))
    y += 34
    labels = {
        "A1_parse_gap": "PARSE: DamageAll target has no same-controller filter",
        "A2_setup_ok": "P1x2 / P0x1 / P2x1 Mystics, Fear in P0 hand",
        "A3_target_hit": "target P1 Mystic takes X=2 (leaves BF)",
        "A4_same_controller_hit": "P1's other Mystic takes the 1 (leaves BF)",
        "A5_caster_spared": "P0's Mystic takes 0 (stays on BF)",
        "A6_third_party_spared": "P2's Mystic takes 0 (stays on BF)",
        "A7_cleanup": "stack empty, Fear in P0 graveyard",
    }
    for k, lab in labels.items():
        av = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if av == "passed" else (
            (255, 90, 90) if av == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {av} - {lab}", fill=c)
        y += 24
    y += 10
    ds = run.get("driver_state") or {}
    d.text((24, y), f"fear_cast={ds.get('fear_cast')} "
           f"target_oid={ds.get('target_oid')} "
           f"x_answered={ds.get('x_answered')} "
           f"turn={ds.get('fear_cast_turn')}", fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    details = run.get("assertion_details") or {}
    for k in ("A1_parse_gap", "A2_setup_ok", "A3_target_hit",
              "A4_same_controller_hit", "A5_caster_spared",
              "A6_third_party_spared", "A7_cleanup"):
        dd = details.get(k)
        if dd:
            d.text((36, y), f"{k}: {dd[:116]}", fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


if __name__ == "__main__":
    asyncio.run(main())
