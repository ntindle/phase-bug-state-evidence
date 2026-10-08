#!/usr/bin/env python3
"""Issue #6951: [Card Bug] Elspeth Conquers Death - chapter II noncreature
tax and chapter III return-and-put-counter are flagged unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Elspeth Conquers Death ({3}{W}{W} Enchantment - Saga):
    "(As this Saga enters and after your draw step, add a lore counter.
     Sacrifice after III.)
     I - Exile target permanent an opponent controls with mana value 3 or
         greater.
     II - Noncreature spells your opponents cast cost {2} more to cast
          until your next turn.
     III - Return target creature or planeswalker card from your graveyard
           to the battlefield. Put a +1/+1 counter or a loyalty counter
           on it."

Reported symptom (v0.42.0, deckbuilder): the deck builder flags the card as
unsupported: `Effect:noncreature, Effect:put`. Chapter II's noncreature-spell
tax and chapter III's "put a +1/+1 counter or a loyalty counter on it"
don't parse.

Parse state on v0.103.0 (observed 2026-10-08 before the run):
  triggers[1] (Chapter 2): execute.effect.type == "Unimplemented",
    name == "unrecognized_clause_head",
    description == "Noncreature spells your opponents cast cost {2} more
    to cast" -> STILL unsupported (reported Effect:noncreature flag
    persists; on v0.81.3 the name was "noncreature").
  triggers[2] (Chapter 3): ChangeZone Graveyard->Battlefield (creature-or-
    planeswalker target) with a typed sub_ability chain:
    TargetOnly(ParentTarget) -> ChooseOneOf[ PutCounter P1P1 | PutCounter
    loyalty ] on ParentTarget. No Unimplemented nodes -> the reported
    Effect:put flag no longer applies at parse level.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Elspeth Conquers Death, 12x Grizzly Bears, 24x Plains, 20x Forest.
  P1: 12x Lightning Bolt, 48x Mountain.
  P0 casts a Bear when able; P1 Bolts it (Bear -> P0 graveyard).
  P0 casts ECD {3}{W}{W} once 5+ lands are out and the Bear is in the yard
  (engine Auto payment). Chapter I fizzles (P1 controls no MV>=3
  permanent). Chapter II is Unimplemented (no-op). In the chapter-II tax
  window P1 Bolts P0's face; the tax is unimplemented so the Bolt must cost
  {R} (1 Mountain tapped). Chapter III: target the Bear in P0's graveyard,
  it returns, then the counter-choice is offered; choose +1/+1 -> Bear 3/3
  with a +1/+1 counter. ECD is sacrificed after III.

Assertions:
  A1_ch2_unimplemented  card-data: chapter II effect is Unimplemented.
                        passed => the reported chapter-II gap persists
                        (deck-builder flag still applies).
  A2_ch3_typed          card-data: chapter III subtree has no Unimplemented
                        nodes and carries the ChooseOneOf counter choice.
                        passed => chapter-III parse gap closed.
  A3_reached_ch3        PRE: ECD at lore 3 (or P0 turn >= cast+4) with
                        the Bear in P0's graveyard and the chapter-3
                        sequence in motion. (The engine auto-targets a
                        sole legal target, so the yard-Bear target prompt
                        may never appear; the counter-choice prompt firing
                        is then the sequence evidence.)
  A4_tax_consistent     engine behavior consistent with the parse: if A1
                        passed (ch2 Unimplemented), P1's in-window Bolt
                        tapped exactly 1 Mountain (no tax); if A1 failed
                        (ch2 now typed), it must have tapped 3 (tax
                        applied). FAILED = engine ignores the parse.
  A5_returned           POST: the Bear is on P0's battlefield.
  A6_counter            POST: that Bear is 3/3 with a +1/+1 counter.
  A7_cleanup            POST: stack empty, game proceeds.

Verdict rule: blocked iff A3 fails (never reached chapter III).
  reproduced iff A1 passes (chapter-II unsupported aspect persists on
    v0.103.0), or A2/A4/A5/A6 fail (parse regression, engine ignoring a
    typed tax, or chapter-III regression - a clearly identified related
    failure).
  not-reproduced iff A1 fails and A2-A7 all pass.
  blocked otherwise.

Evidence: evidence/6951/<run-id>/pre.json, mid.json, post.json, run.json,
assertions.json, observations.json, data_evidence.json, manifest.sha256,
summary.png, scenario_6951_01030.py, wire_log.jsonl, scenario_run.log,
server.log
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6951
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6951")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

PARSE = {"ch2_effect": None, "ch3_blob": None, "ok1": False, "ok2": False}


def check_data_level():
    """Record the v0.103.0 parse of ECD's saga chapters II and III.

    A1 contract: chapter II's effect is Unimplemented (the reported
    Effect:noncreature flag persists). A2 contract: chapter III's subtree
    has no Unimplemented nodes and carries the ChooseOneOf counter choice.
    """
    c = CARD_DATA.get("elspeth conquers death", {})
    trigs = c.get("triggers", [])
    ch2 = next((t for t in trigs if t.get("saga_chapter") == 2), {})
    ch3 = next((t for t in trigs if t.get("saga_chapter") == 3), {})
    ch2eff = ((ch2.get("execute") or {}).get("effect") or {})
    PARSE["ch2_effect"] = ch2eff
    ok1 = ch2eff.get("type") == "Unimplemented"
    ch3blob = json.dumps(ch3, default=str)
    PARSE["ch3_blob"] = ch3blob
    has_unimpl = '"Unimplemented"' in ch3blob
    has_choice = ("ChooseOneOf" in ch3blob and "PutCounter" in ch3blob
                  and "P1P1" in ch3blob and "loyalty" in ch3blob)
    ok2 = (not has_unimpl) and has_choice
    PARSE["ok1"] = ok1
    PARSE["ok2"] = ok2
    out = {
        "name": c.get("name"),
        "oracle_text": c.get("oracle_text"),
        "chapter2_effect": ch2eff,
        "chapter2_unimplemented": ok1,
        "chapter3_has_unimplemented": has_unimpl,
        "chapter3_has_chooseoneof_putcounter": has_choice,
        "chapter3_typed": ok2,
        "note": ("On v0.81.3 ch2 was Unimplemented/noncreature and ch3 "
                 "was fully typed; on v0.103.0 ch2 is "
                 "Unimplemented/unrecognized_clause_head and ch3 remains "
                 "typed."),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    say(f"data-level: ch2_unimplemented={ok1} ch3_typed={ok2}")
    wire("data_level", {"ch2_unimplemented": ok1, "ch3_typed": ok2,
                        "ch2_effect": ch2eff})
    return out


ECD_T = "Elspeth Conquers Death"
ECD_L = "elspeth conquers death"
BEAR_T = "Grizzly Bears"
BEAR_L = "grizzly bears"
BOLT_T = "Lightning Bolt"
BOLT_L = "lightning bolt"
PLAINS_L = "plains"
FOREST_L = "forest"
MOUNTAIN_L = "mountain"
LANDS = (PLAINS_L, FOREST_L, MOUNTAIN_L)

P0_DECK = ((ECD_T, 4), (BEAR_T, 12), ("Plains", 24), ("Forest", 20))
P1_DECK = ((BOLT_T, 12), ("Mountain", 48))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"bear_cast": False, "bolt1_cast": False, "bolt1_done": False,
      "ecd_cast": False, "ecd_cast_turn": None, "ecd_oid": None,
      "ch2_seen": False, "ch2_at": None, "mid_exported": False,
      "bolt2_cast": False, "bolt2_turn": None, "bolt2_done": False,
      "bolt2_tapped": None, "ch3_seen": False, "ch3_at": None,
      "pre_exported": False, "ch3_targeted": False,
      "counter_answered": False, "counter_choice": None,
      "post_at": None, "post_exported": False,
      "target_sels": []}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "target_selections": []}


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
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


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


def tapped_mountains(state, pid):
    n = 0
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == str(pid)
                and o.get("tapped")
                and obj_lname(state, oid) == MOUNTAIN_L):
            n += 1
    return n


def counters_of(obj):
    c = obj.get("counters")
    if isinstance(c, dict):
        return c
    if isinstance(c, list):
        out = {}
        for e in c:
            if isinstance(e, dict):
                k = e.get("type") or e.get("kind") or e.get("name")
                out[str(k)] = e.get("count", 1)
            else:
                out[str(e)] = out.get(str(e), 0) + 1
        return out
    return {}


def saga_lore(state, oid):
    o = get_obj(state, oid)
    for k, v in counters_of(o).items():
        if "lore" in str(k).lower():
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return 0


def has_p1p1(obj):
    for k, v in counters_of(obj).items():
        kl = str(k).lower().replace(" ", "")
        if "p1p1" in kl or "+1/+1" in kl or "plusoneplusone" in kl:
            try:
                if int(v) >= 1:
                    return True
            except (TypeError, ValueError):
                return True
    return False


def stack_empty(state):
    return not (state.get("stack") or [])


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
    need = 3 if pid == 0 else 2
    keep = n_lands >= need or n >= 2
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


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm in LANDS:
        return 0
    if nm == ECD_L:
        return 1
    if nm == BEAR_L:
        return 2
    return 3  # Bolt kept last for P1


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = cand_oid(ch)
            if ref is not None:
                ref_of[ref] = ch["id"]
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o),
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


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


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
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower():
        opp = find_relations_op(st)
        if opp is not None:
            iid = opp.get("interactionId")
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi relations opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
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
    """Yield (opp, rtype, spec_type) for every viewer_interaction
    opportunity that looks like a target selection: schema select/sequence
    with candidates, or exactChoices whose choices carry candidate/target
    codes (passPriority menus excluded)."""
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


def target_opportunity(st):
    """First target-looking opportunity (see iter_target_opps)."""
    for t in iter_target_opps(st):
        return t
    return None, None, None


def record_target_sel(state, opp, stage):
    """Record a target-selection opportunity once per interactionId."""
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
                    f"seat={x['seat']})" for x in cand_info[:8]))
    return True


def pick_bear_bf(state, opp):
    """Bolt1: the P0 Grizzly Bears on the battlefield."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        oid = cand_oid(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if (obj_lname(state, oid) == BEAR_L
                and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "0"):
            return ch
    return None


def pick_player0(state, opp):
    """Bolt2: P0 the player (face)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        if cand_seat(ch) == 0:
            return ch
        oid = cand_oid(ch)
        if oid is not None:
            o = get_obj(state, oid)
            if o.get("zone") == "Player" or o.get("type") == "Player" \
                    or str(o.get("controller", "")) == "0" \
                    and o.get("zone") not in ("Battlefield", "Graveyard",
                                              "Hand", "Library", "Exile",
                                              "Stack"):
                return ch
        blob = (choice_text(ch) + " " + json.dumps(ch, default=str)).lower()
        if "player" in blob and ("opponent" in blob or "you" in blob):
            return ch
    return None


def pick_bear_yard(state, opp):
    """Chapter III: the P0 Grizzly Bears in the graveyard."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        oid = cand_oid(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if (obj_lname(state, oid) == BEAR_L
                and o.get("zone") == "Graveyard"
                and str(o.get("owner", o.get("controller", -1))) == "0"):
            return ch
    return None


async def answer_target(c, state, opp, rtype, spec_type, ch, tag, stage):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    oid = cand_oid(ch)
    say(f"[{tag}] answering {stage} target: "
        f"{obj_lname(state, oid) if oid else choice_text(ch)[:40]} "
        f"(oid {oid}) via {sub['response']['type']}")
    wire("target_answer", {"who": tag, "stage": stage, "iid": iid,
                           "oid": oid, "submission": sub})
    await answer_vi(c, opp, ch, tag)
    return oid


async def bolt_target_tick(c, tag, st, state):
    """Answer Bolt target prompts: bolt1 -> P0's Bear on BF; bolt2 -> P0."""
    if ST["bolt1_cast"] and not ST["bolt1_done"]:
        for opp, rtype, spec_type in iter_target_opps(st):
            record_target_sel(state, opp, "bolt1")
            pick = pick_bear_bf(state, opp)
            if pick is None:
                continue
            await answer_target(c, state, opp, rtype, spec_type, pick, tag,
                                "bolt1")
            ST["bolt1_done"] = True
            return True
        say(f"[{tag}] bolt1 prompt has no P0-Bear candidate; not answering")
        OBS["unexpected_prompts"].append({"who": tag, "stage": "bolt1",
                                          "note": "no bear candidate"})
        return False
    if ST["bolt2_cast"] and not ST["bolt2_done"]:
        for opp, rtype, spec_type in iter_target_opps(st):
            record_target_sel(state, opp, "bolt2")
            pick = pick_player0(state, opp)
            if pick is None:
                continue
            await answer_target(c, state, opp, rtype, spec_type, pick, tag,
                                "bolt2")
            ST["bolt2_done"] = True
            return True
        say(f"[{tag}] bolt2 prompt has no P0-player candidate; not answering")
        OBS["unexpected_prompts"].append({"who": tag, "stage": "bolt2",
                                          "note": "no player candidate"})
        return False
    return False


async def ch3_target_tick(c, tag, st, state):
    """Answer the chapter-III target prompt (Bear in P0's graveyard).

    PRE is exported BEFORE answering: the chapter-III precondition state.
    """
    if not ST["ch3_seen"] or ST["ch3_targeted"]:
        return False
    for opp, rtype, spec_type in iter_target_opps(st):
        # Sanity: this prompt must offer the yard Bear; anything else is
        # wired and left unanswered (e.g. the chapter-I trigger, which has
        # no legal targets and must fizzle on its own).
        if pick_bear_yard(state, opp) is None:
            record_target_sel(state, opp, "unexpected-target")
            say(f"[{tag}] target prompt is not the ch3 yard-Bear prompt; "
                f"leaving unanswered")
            continue
        record_target_sel(state, opp, "ch3")
        if not ST["pre_exported"]:
            await do_export(c, "pre.json")
            ST["pre_exported"] = True
            say("PRE exported: chapter III pending, Bear in P0 yard")
        pick = pick_bear_yard(state, opp)
        await answer_target(c, state, opp, rtype, spec_type, pick, tag, "ch3")
        ST["ch3_targeted"] = True
        return True
    return False


def opt_index_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "optionIndex" \
                and "value" in d:
            return str(d["value"])
    return None


async def counter_choice_tick(c, tag, st, state):
    """Answer the chapter-III ChooseOneOf counter prompt.

    106 surface (observed 2026-10-08): response.type == "exactChoices",
    two choices, each with surfaces:
      {"type":"summary","data":{"code":"candidate"}},
      {"type":"action","data":{"code":"chooseBranch","actionId":...}},
      {"type":"value","data":{"role":"optionIndex","value":"0"|"1"}}.
    No branch text; optionIndex 0 = PutCounter P1P1 per the v0.103.0
    card-data parse order (branches[0] = "put a +1/+1 counter",
    branches[1] = loyalty), confirmed by the v0.81.3 run's A6.

    The engine auto-targets the yard Bear when it is the only legal
    target, so ch3_targeted may never be set; gate on ch3_seen instead.
    """
    if not ST["ch3_seen"] or ST["counter_answered"]:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if len(chs) != 2:
            continue
        codes = set()
        for ch in chs:
            codes.update(cc for cc in surf_codes(ch) if cc)
        if "chooseBranch" not in codes:
            continue
        iid = opp.get("interactionId")
        wire("counter_choice",
             {"who": tag, "iid": iid,
              "choices": [{"id": ch.get("id"),
                           "optionIndex": opt_index_of(ch),
                           "text": choice_text(ch)[:60]} for ch in chs],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        # Prefer +1/+1 by text; fall back to optionIndex 0 (parse order).
        pick = next((ch for ch in chs
                     if "+1/+1" in choice_text(ch).lower()), None)
        if pick is None:
            pick = next((ch for ch in chs if opt_index_of(ch) == "0"),
                        chs[0])
        ST["counter_choice"] = f"optionIndex {opt_index_of(pick)}"
        ST["counter_answered"] = True
        # Auto-target case: PRE may not have been exported yet (no yard-
        # Bear target prompt ever appeared). Export it now, before
        # answering, documenting the precondition as best as possible.
        if not ST["pre_exported"]:
            await do_export(c, "pre.json")
            ST["pre_exported"] = True
            say("PRE exported: at counter-choice prompt (engine "
                "auto-targeted the yard Bear)")
        say(f"[{tag}] counter choice: picked '{ST['counter_choice']}'")
        OBS["target_selections"].append({"stage": "counter_choice",
                                         "pick": ST["counter_choice"]})
        await answer_vi(c, opp, pick, tag)
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
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    # track ECD on BF + lore chapters
    ch3_just_seen = False
    ecds = bf_oids(state, 0, ECD_L)
    if ecds and ST["ecd_oid"] is None:
        ST["ecd_oid"] = ecds[0]
        say(f"ECD on BF: oid={ecds[0]}")
        wire("ecd_entered", {"oid": ecds[0]})
    if ST["ecd_oid"] is not None:
        lore = saga_lore(state, ST["ecd_oid"])
        if lore >= 2 and not ST["ch2_seen"]:
            ST["ch2_seen"] = True
            ST["ch2_at"] = time.time()
            say(f"chapter II reached (lore={lore})")
            wire("chapter2", {"lore": lore})
        if lore >= 3 and not ST["ch3_seen"]:
            ST["ch3_seen"] = True
            ST["ch3_at"] = time.time()
            ch3_just_seen = True
            say(f"chapter III reached (lore={lore})")
            wire("chapter3", {"lore": lore})
    # turn-arithmetic fallback if lore counters don't surface
    turn = state.get("turn_number") or 0
    if (ST["ecd_cast_turn"] is not None and not ST["ch2_seen"]
            and turn >= ST["ecd_cast_turn"] + 2 and ST["ecd_oid"]):
        ST["ch2_seen"] = True
        ST["ch2_at"] = time.time()
        say("chapter II reached (turn-arithmetic fallback)")
        wire("chapter2", {"via": "turn_fallback"})
    if (ST["ecd_cast_turn"] is not None and not ST["ch3_seen"]
            and turn >= ST["ecd_cast_turn"] + 4 and ST["ecd_oid"]):
        ST["ch3_seen"] = True
        ST["ch3_at"] = time.time()
        ch3_just_seen = True
        say("chapter III reached (turn-arithmetic fallback)")
        wire("chapter3", {"via": "turn_fallback"})
    # PRE at the ch3 transition, before any target prompt is answered:
    # ECD at lore 3 with the Bear still in P0's yard is the ideal
    # precondition. (The engine auto-targets a sole legal target, so the
    # yard-Bear target prompt may never appear; counter_choice_tick has a
    # PRE backup for that case.)
    if ch3_just_seen and not ST["pre_exported"] \
            and yard_oids(state, 0, BEAR_L):
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported: chapter III reached, Bear in P0 yard")

    # chapter-III target (exports PRE before answering)
    if await ch3_target_tick(c, tag, st, state):
        return True
    # chapter-III counter choice
    if await counter_choice_tick(c, tag, st, state):
        return True

    # MID: chapter II seen, stack empty, 6s grace
    if (ST["ch2_seen"] and not ST["mid_exported"]
            and stack_empty(state)
            and time.time() - (ST["ch2_at"] or 0) > 6):
        await do_export(c, "mid.json")
        ST["mid_exported"] = True
        say("MID exported: after chapter II")
        st = st_of(c) or st
        state = st["state"]
        acts = merged_actions(st)

    # POST: counter answered, Bear on BF, stack empty, 8s grace
    if (ST["counter_answered"] and bf_oids(state, 0, BEAR_L)
            and not ST["post_exported"] and stack_empty(state)):
        if ST["post_at"] is None:
            ST["post_at"] = time.time()
    if (ST["post_at"] is not None and not ST["post_exported"]
            and time.time() - ST["post_at"] > 8
            and stack_empty(state)):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say("POST exported: Bear returned with counter; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        n_untapped = len(untapped_lands(state, 0))
        hn = hand_lnames(state, 0)
        # 1. cast Grizzly Bears ({1}{G}) once a Bear is in hand
        if (not ST["bear_cast"] and BEAR_L in hn and n_untapped >= 2
                and not bf_oids(state, 0, BEAR_L)
                and not yard_oids(state, 0, BEAR_L)):
            a = find_cast_action(acts, state, BEAR_L)
            if a is not None:
                ST["bear_cast"] = True
                say(f"[{tag}] casting {BEAR_T} (engine Auto payment)")
                wire("cast_submit", {"tag": "bear"})
                await submit_as_is(c, a)
                return True
        # 2. cast Elspeth Conquers Death ({3}{W}{W}) once the Bear is in
        # the yard and 5+ lands are out
        if (not ST["ecd_cast"] and ECD_L in hn and n_untapped >= 5
                and yard_oids(state, 0, BEAR_L)):
            a = find_cast_action(acts, state, ECD_L)
            if a is not None:
                ST["ecd_cast"] = True
                ST["ecd_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] casting {ECD_T} on turn "
                    f"{ST['ecd_cast_turn']} (engine Auto payment)")
                wire("cast_submit", {"tag": "ecd",
                                    "turn": ST["ecd_cast_turn"]})
                await submit_as_is(c, a)
                return True
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
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
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await bolt_target_tick(c, tag, st, state):
        return True

    # measure mana paid for bolt2 right after the cast resolves
    if (ST["bolt2_cast"] and ST["bolt2_tapped"] is None
            and ST["bolt2_done"] and stack_empty(state)):
        ST["bolt2_tapped"] = tapped_mountains(state, 1)
        say(f"bolt2 resolved on turn {ST['bolt2_turn']}: P1 tapped "
            f"Mountains = {ST['bolt2_tapped']}")
        wire("bolt2_mana", {"tapped_mountains": ST["bolt2_tapped"],
                            "turn": ST["bolt2_turn"]})

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(top_acts(st)):
        n_untapped = len(untapped_lands(state, 1))
        hn = hand_lnames(state, 1)
        turn = state.get("turn_number") or 0
        # bolt1: kill the Bear as soon as possible
        if (not ST["bolt1_cast"] and BOLT_L in hn and n_untapped >= 1
                and bf_oids(state, 0, BEAR_L)):
            a = find_cast_action(acts, state, BOLT_L)
            if a is not None:
                ST["bolt1_cast"] = True
                say(f"[{tag}] casting {BOLT_T} #1 at the Bear "
                    f"(engine Auto payment)")
                wire("cast_submit", {"tag": "bolt1"})
                await submit_as_is(c, a)
                return True
        # bolt2: in the chapter-II tax window (P1's turn right after the
        # chapter-II turn), Bolt P0's face and measure the cost
        in_window = (ST["ecd_cast_turn"] is not None
                     and turn == ST["ecd_cast_turn"] + 3)
        if (ST["ch2_seen"] and in_window and not ST["bolt2_cast"]
                and BOLT_L in hn and n_untapped >= 1):
            a = find_cast_action(acts, state, BOLT_L)
            if a is not None:
                ST["bolt2_cast"] = True
                ST["bolt2_turn"] = turn
                say(f"[{tag}] casting {BOLT_T} #2 at P0 face IN the "
                    f"chapter-II window (turn {turn})")
                wire("cast_submit", {"tag": "bolt2", "turn": turn})
                await submit_as_is(c, a)
                return True
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


# ------------------------------------------------------------- main loop

async def main():
    t0 = time.time()
    last_rev_change = t0
    game_started = False

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P06951r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16951r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
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
                await tick(c, tag)
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
            ecd = ST["ecd_oid"]
            lore = saga_lore(state, ecd) if ecd else "-"
            stk = len(state.get("stack") or [])
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} pp={state.get('priority_player')} "
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"P1untapped={len(untapped_lands(state, 1))} "
                f"P0life={life_of(state, 0)} P1life={life_of(state, 1)} "
                f"ecd={ecd} lore={lore} "
                f"bear_bf={bf_oids(state, 0, BEAR_L)} "
                f"bear_yard={yard_oids(state, 0, BEAR_L)} "
                f"stack={stk} bear_cast={ST['bear_cast']} "
                f"bolt1={ST['bolt1_cast']}/{ST['bolt1_done']} "
                f"ecd_cast={ST['ecd_cast']}@{ST['ecd_cast_turn']} "
                f"ch2={ST['ch2_seen']} bolt2={ST['bolt2_cast']}/"
                f"{ST['bolt2_done']}/{ST['bolt2_tapped']} "
                f"ch3={ST['ch3_seen']}/{ST['ch3_targeted']}/"
                f"{ST['counter_answered']} "
                f"pre={ST['pre_exported']} mid={ST['mid_exported']} "
                f"post={ST['post_exported']}")

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    await finish(p0, p1, t0, hello, data_level)


async def finish(p0, p1, t0, hello, data_level):
    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_s = load("mid.json")
    post_s = load("post.json")

    # A1: card-data chapter II still Unimplemented
    A["A1_ch2_unimplemented"] = "passed" if PARSE["ok1"] else "failed"
    eff = PARSE["ch2_effect"] or {}
    D["A1_ch2_unimplemented"] = (
        f"chapter II effect = {eff.get('type')}/{eff.get('name')} "
        f"(expected Unimplemented/*; the reported Effect:noncreature "
        f"deck-builder flag persists)")

    # A2: card-data chapter III fully typed
    A["A2_ch3_typed"] = "passed" if PARSE["ok2"] else "failed"
    D["A2_ch3_typed"] = (
        f"chapter III typed={PARSE['ok2']} "
        f"(expected no Unimplemented + ChooseOneOf/PutCounter choice)")

    # A3: reached chapter III (PRE with ECD at lore 3, Bear in P0 yard)
    if pre_s is not None:
        ecds = bf_oids(pre_s, 0, ECD_L)
        lore = saga_lore(pre_s, ecds[0]) if ecds else None
        turn = pre_s.get("turn_number")
        bear_yard = bool(yard_oids(pre_s, 0, BEAR_L))
        lore_ok = (lore == 3) or (
            ST["ecd_cast_turn"] is not None
            and turn is not None and turn >= ST["ecd_cast_turn"] + 4)
        # Ideal precondition: ECD at lore 3 with the Bear still in the
        # yard. Auto-target case: the engine skips the yard-Bear target
        # prompt, so PRE is taken at the counter-choice prompt with the
        # Bear already back; the ch3 sequence firing (targeted or
        # counter_answered) is the evidence instead.
        seq_fired = ST["ch3_targeted"] or ST["counter_answered"]
        ok = bool(ecds) and lore_ok and ST["ch3_seen"] \
            and (bear_yard or seq_fired)
        A["A3_reached_ch3"] = "passed" if ok else "failed"
        D["A3_reached_ch3"] = (
            f"ecd={ecds[0] if ecds else None} lore={lore} turn={turn} "
            f"(cast turn {ST['ecd_cast_turn']}) bear_in_yard={bear_yard} "
            f"ch3_seen={ST['ch3_seen']} ch3_targeted={ST['ch3_targeted']} "
            f"counter_answered={ST['counter_answered']}")
    else:
        A["A3_reached_ch3"] = "failed"
        D["A3_reached_ch3"] = "pre.json missing"

    # A4: engine behavior consistent with the parse
    if ST["bolt2_tapped"] is None:
        A["A4_tax_consistent"] = "not-run"
        D["A4_tax_consistent"] = (
            f"bolt2 never resolved in-window (cast={ST['bolt2_cast']} "
            f"done={ST['bolt2_done']}); tapped-mana not measured")
    else:
        expected = 1 if PARSE["ok1"] else 3
        ok = ST["bolt2_tapped"] == expected
        A["A4_tax_consistent"] = "passed" if ok else "failed"
        D["A4_tax_consistent"] = (
            f"P1 in-window Bolt (turn {ST['bolt2_turn']}) tapped "
            f"{ST['bolt2_tapped']} Mountain(s); expected {expected} "
            f"({'no tax: ch2 Unimplemented' if PARSE['ok1'] else 'tax {2} applied: ch2 typed'})")

    # A5/A6: Bear returned with +1/+1 counter
    if post_s is not None:
        bears = [(oid, get_obj(post_s, oid))
                 for oid in bf_oids(post_s, 0, BEAR_L)]
        ok5 = len(bears) >= 1
        A["A5_returned"] = "passed" if ok5 else "failed"
        D["A5_returned"] = f"P0 Bears on BF in post: {len(bears)}"
        ok6 = False
        for oid, o in bears:
            pw, tw = o.get("power"), o.get("toughness")
            p1p1 = has_p1p1(o)
            D.setdefault("A6_counter", "")
            D["A6_counter"] += (f"bear oid={oid} P/T={pw}/{tw} "
                                f"+1/+1={p1p1} counters={counters_of(o)}; ")
            if pw == 3 and tw == 3 and p1p1:
                ok6 = True
        A["A6_counter"] = "passed" if ok6 else "failed"
        if "A6_counter" not in D:
            D["A6_counter"] = "no bears on BF"
    else:
        A["A5_returned"] = "failed"
        A["A6_counter"] = "failed"
        D["A5_returned"] = "post.json missing"
        D["A6_counter"] = "post.json missing"

    # A7: cleanup (waiting_for is null on 106; stack empty = proceeding)
    if post_s is not None:
        stack_clear = stack_empty(post_s)
        A["A7_cleanup"] = "passed" if stack_clear else "failed"
        D["A7_cleanup"] = (f"stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A7_cleanup"] = "failed"
        D["A7_cleanup"] = "post.json missing"

    # ---------------------------------------------------------- verdict
    keys = ("A1_ch2_unimplemented", "A2_ch3_typed", "A3_reached_ch3",
            "A4_tax_consistent", "A5_returned", "A6_counter", "A7_cleanup")
    if A["A3_reached_ch3"] != "passed":
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: never reached chapter III")
    elif A["A1_ch2_unimplemented"] == "failed" \
            and A["A4_tax_consistent"] == "not-run":
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: ch2 parse changed but the "
                            "in-game tax could not be measured")
    elif A["A1_ch2_unimplemented"] == "passed" \
            or A["A2_ch3_typed"] == "failed" \
            or A["A4_tax_consistent"] == "failed" \
            or A["A5_returned"] == "failed" \
            or A["A6_counter"] == "failed":
        verdict = "reproduced"
        OBS["notes"].append(
            "verdict=reproduced: chapter-II noncreature tax still "
            "Unimplemented on v0.103.0 (deck-builder Effect:noncreature "
            "flag persists)"
            if A["A1_ch2_unimplemented"] == "passed" else
            "verdict=reproduced: related failure (see failed assertions)")
    elif all(A.get(k) == "passed" for k in keys):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: incomplete assertion chain")

    for k in keys:
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "target_selections": OBS["target_selections"],
                   "unexpected_prompts": OBS["unexpected_prompts"],
                   "auto_answered": OBS["auto_answered"],
                   "tick_errors": OBS["tick_errors"],
                   "rejections": OBS["rejections"],
                   "notes": OBS["notes"]}, f, indent=1, default=str)

    date_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "date": date_iso,
        "game_code": STAGE.get("game_code"),
        "server": {
            "server_version": hello.get("server_version"),
            "build_commit": hello.get("build_commit"),
            "protocol_version": hello.get("protocol_version"),
            "mode": hello.get("mode"),
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {"P0": [[n, c] for n, c in P0_DECK],
                  "P1": [[n, c] for n, c in P1_DECK]},
        "format_config": "default Bo1 (2 human-client seats)",
        "setup_line": ("P0 4x Elspeth Conquers Death / 12x Grizzly Bears / "
                       "24x Plains / 20x Forest; P1 12x Lightning Bolt / "
                       "48x Mountain. P0 casts a Bear; P1 Bolts it. P0 casts "
                       "ECD {3}{W}{W} (engine Auto payment) once 5+ lands "
                       "are out and the Bear is in the yard. Chapter I "
                       "fizzles (no MV>=3 permanent); chapter II is "
                       "Unimplemented (no-op); P1 Bolts P0's face in the "
                       "chapter-II window; chapter III returns the Bear "
                       "and offers the +1/+1-or-loyalty choice."),
        "contract_line": ("Chapter II must still parse as Unimplemented "
                          "(the reported deck-builder Effect:noncreature "
                          "flag persists -> reproduced). Chapter III must "
                          "still parse typed and work in-game: the Bear "
                          "returns 3/3 with a +1/+1 counter. The in-window "
                          "Bolt's tapped-mana count must be consistent "
                          "with the parse (1 Mountain if Unimplemented, "
                          "3 if the tax is now typed)."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6951.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A7 and the issue-specific game plan unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably, so no driver mana taps (driver taps on top of "
            "engine auto-taps double-pay).",
            "Target prompts answered via the 106 target_opportunity "
            "helper (schema select/sequence with candidates, or "
            "exactChoices with candidate/target codes); the chapter-I "
            "prompt (no legal targets) is wired but never answered - "
            "it must fizzle on its own.",
            "The chapter-III ChooseOneOf counter prompt has no 106 "
            "precedent; the driver wires the full opportunity and "
            "prefers the +1/+1-textured choice, falling back to "
            "optionIndex 0 (parse order: branches[0] = PutCounter "
            "P1P1).",
            "Chapter-II detection is lore-counter-first with a "
            "turn-arithmetic fallback (ch2 on P0's turn cast+2); the "
            "in-window Bolt is gated to P1's turn cast+3.",
            "Pre/mid/post states are authoritative exports (data.state "
            "parsed once from the export envelope) via the host client "
            "only; the reported OUTCOME is asserted on the saved "
            "states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5652442626,
        "limitations": [
            "Browser UI / deckbuilder UI not exercised; the deck-builder "
            "'unsupported' flag is evidenced via the card-data parse "
            "check (Unimplemented nodes drive the flag).",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via full "
            "game replay).",
        ],
        "mulligans": STAGE["mulls"],
        "rejections": OBS["rejections"],
        "notes": OBS["notes"],
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"wrote run.json verdict={verdict}")

    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6951_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6951_01030.py into EVDIR")

    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied server.log into EVDIR")
    except Exception as e:
        say(f"server.log copy failed: {e} "
            f"(shared pinned server; run log kept in scenario_run.log)")

    render_summary(run, pre_s, mid_s, post_s)

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    # Final manifest AFTER RUNLOG is closed: write_manifest()'s own say()
    # is then stdout-only (guarded on closed RUNLOG), so no logged line can
    # go stale after the hash.
    write_manifest()
    say("scenario finished")


def render_summary(run, pre_s, mid_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 980
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6951 - Elspeth Conquers Death",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "saga chapters II+III",
           fill=(140, 160, 180))
    y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states / card-data):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_ch2_unimplemented": "card-data: chapter II still Unimplemented",
        "A2_ch3_typed": "card-data: chapter III fully typed (ChooseOneOf)",
        "A3_reached_ch3": "PRE: ECD lore 3, Bear in P0 yard, ch3 pending",
        "A4_tax_consistent": "P1 in-window Bolt cost consistent w/ parse",
        "A5_returned": "POST: Bear back on P0 battlefield",
        "A6_counter": "POST: Bear 3/3 with +1/+1 counter",
        "A7_cleanup": "stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Bear / ECD across states:", fill=(200, 210, 225))
    y += 24
    for label, st in (("pre ", pre_s), ("mid ", mid_s), ("post", post_s)):
        if st is not None:
            bears = [(oid, get_obj(st, oid))
                     for oid in bf_oids(st, 0, BEAR_L)]
            ecds = bf_oids(st, 0, ECD_L)
            lore = saga_lore(st, ecds[0]) if ecds else "-"
            line = (f"{label}: ecd={ecds[0] if ecds else None} lore={lore} "
                    f"bears_bf={len(bears)} "
                    + (" ".join(f"{o.get('power')}/{o.get('toughness')}"
                                for _, o in bears) if bears else ""))
        else:
            line = f"{label}: (no state)"
        d.text((36, y), line[:112], fill=(150, 160, 175))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    notes = (run["notes"] or [])[:2]
    details = [run["assertion_details"].get(k, "") for k in labels]
    for n in notes + details:
        for seg in [str(n)[i:i + 116] for i in range(0, len(str(n)), 116)][:2]:
            d.text((36, y), seg, fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    files = ["pre.json", "mid.json", "post.json",
             "run.json", "assertions.json", "observations.json",
             "data_evidence.json", "scenario_6951_01030.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
