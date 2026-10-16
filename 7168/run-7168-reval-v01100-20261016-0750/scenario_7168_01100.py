#!/usr/bin/env python3
"""Issue #7168 re-validation on v0.110.0 (protocol 142): Ivy, Gleeful
Spellthief -- copy doesn't target Ivy.

Oracle: "Flying. Whenever a player casts a spell that targets only a single
creature other than Ivy, you may copy that spell. The copy targets Ivy.
(A copy of an Aura spell becomes a token.)"

Reported (Discord): the spell is correctly copied, but the copy does NOT
target Ivy.

Parse (pinned v0.110.0 card-data.json): the SpellCast trigger emits
  CopySpell{target: TriggeringSource, retarget: KeepOriginalTargets}
and "The copy targets ~" is an Unimplemented sub-ability
(unrecognized_clause_head). The retarget-to-Ivy child is unsupported, so the
copy keeps the original target -- same shape as v0.83.0.

Prior run (2026-09-15, v0.83.0/protocol 70, run 20260915-7168d): reproduced
- A1-A3, A5-A7 passed; A4 FAILED (copy targets kept the Bear, never Ivy).
Published + comment maintained.

This script is a protocol-142 port of scenario_7168.py (driver conventions
from the verified protocol-142 scenario_7166_01100.py):
  - waiting_for is gone (null); prompt detection via
    viewer_interaction.waitingForKind.code + opportunities (canSubmit).
  - PlayLand/CastSpell/ActivateAbility live in legal_actions_by_object;
    merged_actions() merges them with _src_oid (plain top-level
    legal_actions hides them).
  - CastSpell submitted EXACTLY as advertised (no target injection); the
    Bolt's target IS answered via vi schema target selection (P0's Bear).
  - Ivy's may-copy surfaces as an exactChoices decideOptionalEffect
    opportunity (proven shape from scenario_301_01030.py); accept = choice
    whose surface has role "accept" and value "true". No blob-text gate
    (per #7195 the choice text may be absent).
  - If the engine raises CopyRetarget for the copy (waitingForKind.code ==
    "CopyRetarget"), record it and answer with Ivy (the correct target) so
    the flow completes; A5 records the prompt.
  - Priority = PassPriority legal action. Cast-in-flight guard: withhold
    our own PassPriority until the engine processes a submitted CastSpell.

Behavioral contract (2 human seats, native engine, Bo1, protocol 142):
  P0: 12x ivy, gleeful spellthief / 8x grizzly bears / 20x forest / 20x island
      (dense Ivy playset: harness convenience so the fixture does not
      depend on a 4-of draw streak; engine accepts >4-of for custom
      games). Casts Ivy ({G}{U}), then exactly one Bear ({1}{G}).
  P1: 20x lightning bolt / 40x mountain.
      Casts one Bolt targeting P0's Bear once Ivy is on the BF.
  P0 answers Ivy's optional-copy decision with accept=true.
  Observe the copy on the stack (controller/targets), watch for any
  CopyRetarget prompt, then observe resolution. Ivy is 2/1: she dies iff
  the copy targets her.

  A1 setup_ok          Ivy + one Bear on P0 BF; P1 cast Bolt at the Bear
                       (target answered); pre.json exported.
  A2 trigger_offered   Ivy's optional-copy decision raised for P0.
  A3 copy_created      after accept, a second Bolt spell (controller 0,
                       the copy) observed on the stack.
  A4 copy_targets_ivy  the copy's targets include Ivy's oid. (expect FAILED)
  A5 no_retarget_prompt no CopyRetarget prompt was raised for the copy.
  A6 outcome           post: Ivy alive on BF and the targeted Bear dead --
                       the copy did not hit Ivy.
  A7 cleanup           stack empty, game advances, post.json exported.

Verdict: reproduced iff A1-A3 pass and A4 fails (copy targets lack Ivy).
         not-reproduced iff A1-A5 pass (copy targets Ivy, no retarget
         prompt), or A5 failed but answering the retarget prompt with Ivy
         makes the copy target Ivy (engine now offers retargeting).
         blocked iff A1 or A2 fails.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import websockets as wsmod  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7168-reval-v01100-20261016-0806")
ISSUE = 7168
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

IVY = "Ivy, Gleeful Spellthief"
BEAR = "Grizzly Bears"
BOLT = "Lightning Bolt"
FOREST = "Forest"
ISLAND = "Island"
MOUNTAIN = "Mountain"
P0_DECK = [(IVY, 12), (BEAR, 8), (FOREST, 20), (ISLAND, 20)]
P1_DECK = [(BOLT, 20), (MOUNTAIN, 40)]
TIMEOUT = 900
SETUP_TURN_CAP = 30
LANDS = ("Plains", "Mountain", "Forest", "Swamp", "Island")

CD_PATH = f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "observed_at": "2026-10-16",
    "source": "backfill-owned isolated v0.110.0 single-user server on "
              "127.0.0.1:9374 (pid 97902, run backfill-20261016-0341, "
              "own games.db); ServerHello re-verified at run start; "
              "verified pin (minisign-verify of binary + signed data manifest "
              "with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> BOLT_CAST -> COPY_DECISION -> COPY_WINDOW
                           #   -> RESOLVED -> DONE
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "hello_ok": False,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "parse_rec": None,
        "parse_chain": None,
        "ivy_oid": None,
        "bear_oid": None,
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "bolt_cast": False, "bolt_oid": None, "bolt_stack_id": None,
        "bolt_target_chosen": False,
        "bolt_target_oid": None,
        "copy_decision_seen": False,
        "copy_accepted": False,
        "copy_retarget_seen": False,
        "copy_retarget_answered_ivy": False,
        "saw_two_bolts": False,
        "copy_entry": None,
        "orig_entry": None,
        "copy_stack_id": None,
        "seen_stack_ids": set(),
        "quiescent_ticks": 0,
        "resolve_turn": None,
        "resolve_wall": None,
        "post_turn": None,
        "cast_pending": None,
        "turns_seen": set(), "game_started": False, "game_over": False,
        "stall_observed": False,
        "unexpected": [],
        "notes": [],
        "t0": time.time(),
    })
    WF_SEEN.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("card_name") or o.get("name")
               or o.get("base_name") or "").lower()


def hand_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Hand"
            and str(o.get("controller", -1)) == str(pid)]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_id(state, pid, name):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == name.lower():
            return o
    return None


def find_hand(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name.lower():
            return o
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid, name=None):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o))
            and not get_obj(state, o).get("tapped")
            and (name is None or obj_lname(state, o) == name.lower())]


def untapped_land_count(state, pid):
    return len(untapped_lands(state, pid))


def life(state, pid):
    for p in state.get("players", []):
        if str(p.get("id", p.get("seat", -1))) == str(pid):
            return p.get("life")
    return None


def zone_count(state, owner, zone):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == zone and str(o.get("owner", -1)) == str(owner))


def gy_names(state, owner):
    return [str(o.get("card_name") or o.get("name") or "")
            for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", -1)) == str(owner)]


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def merged_actions(st):
    """Top-level legal_actions + legal_actions_by_object (with _src_oid).
    Protocol 118+: PlayLand/CastSpell/ActivateAbility are hidden unless
    merged."""
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


def action_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def action_type_codes(ch):
    """Codes from action-type surfaces only (summary/value surfaces carry
    marker codes like "candidate" that are not decisions)."""
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if s.get("type") == "action"
            and isinstance(s.get("data"), dict)]


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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def _cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg = {"type": a["type"], "data": a["data"]}
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


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = action_codes(ch)
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def verify_server_hello():
    ws = await wsmod.connect("ws://127.0.0.1:9374/ws", max_size=1_000_000)
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
    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"
    ST["server_hello"] = hello
    ST["hello_ok"] = True


def parse_ivy():
    """Record the pinned card-data parse of Ivy's trigger chain."""
    cd = json.load(open(CD_PATH))
    t = cd.get("ivy, gleeful spellthief")
    if t is None:
        for k, v in cd.items():
            if isinstance(v, dict) and \
                    str(v.get("name") or "").lower() == \
                    "ivy, gleeful spellthief":
                t = v
                break
    rec = {"found": t is not None}
    if t is not None:
        rec["oracle_text"] = t.get("oracle_text")
        tr = (t.get("triggers") or [None])[0]
        ab = (tr.get("execute") or {}) if tr else {}
        chain = []
        unimpl = []
        while ab:
            eff = ab.get("effect") or {}
            et = str(eff.get("type") or eff.get("name"))
            detail = str(eff.get("retarget") or
                         eff.get("description") or eff.get("reason") or "")
            chain.append(et + (f"({detail})" if detail else ""))
            if et.lower() == "unimplemented":
                unimpl.append(detail[:160])
            ab = ab.get("sub_ability")
        rec["effect_chain"] = chain
        rec["unimplemented_nodes"] = unimpl
        ST["parse_chain"] = chain
        with open(f"{EVDIR}/parse_ivy.json", "w") as f:
            json.dump(rec, f, indent=1)
        say(f"parse_ivy.json written; chain: {' -> '.join(chain)}")
    else:
        say("parse check: Ivy NOT FOUND in card-data")
    wire("parse_ivy", rec)
    ST["parse_rec"] = rec
    return rec


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)


def load_state(path):
    try:
        with open(f"{EVDIR}/{path}") as f:
            env = json.load(f)
        return env.get("state", env)
    except Exception:
        return None


# ---------------- preamble (mulligan / bottom / discard / land) -----------
async def do_mulligan(c, acts, st, pid, tag, keep_pred):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in action_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", str(iid or c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    state = st["state"]
    mull_count = sum(1 for k in SUBMITTED_OPPS
                     if k[0] == tag and k[1] == "mull")
    keep = keep_pred(state, pid, mull_count)
    say(f"[{tag}] mulligan #{mull_count}: hand={hand_lnames(state, pid)} -> "
        f"{'keep' if keep else 'redo'}")
    wire("mulligan", {"who": tag, "decision": "keep" if keep else "mulligan",
                      "hand": hand_lnames(state, pid),
                      "iid": str(iid)[:12]})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
    return True


def p0_keep_pred(state, pid, mull_count):
    # Dense Ivy playset (12x) is a test-harness convenience the engine
    # accepts for custom games; the fixture needs Ivy in hand early.
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    return (("ivy, gleeful spellthief" in hn and nlands >= 2)
            or mull_count >= 5)


def p1_keep_pred(state, pid, mull_count):
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    return (("lightning bolt" in hn and nlands >= 2) or mull_count >= 3)


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: gated on waitingForKind.code == 'mulligan' at
    turn 1 / Untap. Prefer the vi schema/select opportunity; fall back to
    the verbatim per-card SelectCards actions."""
    state = st["state"]
    if vi_kind_code(st) != "mulligan":
        return False
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
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    con = None
    if target:
        con = (((target[2].get("data") or {}).get("constraint") or {})
               .get("data") or {})
    else:
        a0 = sel_acts[0].get("data", {}) or {}
        con = a0.get("constraint") or {}
        if isinstance(con, dict):
            con = (con.get("data") or {})
    n = int((con or {}).get("min") or (con or {}).get("max") or 1)
    if n <= 0:
        return False
    key = (tag, "bottom", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    protect = {IVY.lower(), BEAR.lower()} if pid == 0 else {BOLT.lower()}

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (2, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (1, str(o))

    picks = sorted(hand_ids(state, pid), key=rank_oid)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, o) for o in picks]}")
    if target and all(_cand_reference(ch) is not None
            for ch in target[1]):
        opp = target[0]
        id_by_ref = {}
        for ch in target[1]:
            id_by_ref[str(_cand_reference(ch))] = ch.get("id")
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "select",
                            "data": {"choiceIds": [id_by_ref[str(o)] for o in picks
                                                   if str(o) in id_by_ref]}}}
        wire("bottom", {"who": tag, "via": "vi", "picks": picks})
        await interact_as(c, sub, tag)
        return True
    wire("bottom", {"who": tag, "via": "verbatim_selectcards", "picks": picks})
    for a in sel_acts:
        cards = (a.get("data", {}) or {}).get("cards") or []
        if cards and int(cards[0]) in picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(cards[0])]}})
            await asyncio.sleep(0.4)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    protect = {IVY.lower(), BEAR.lower()} if pid == 0 else {BOLT.lower()}
    handset = set(str(x) for x in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(str(_cand_reference(ch)) in handset for ch in cands):
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    opp, cands, spec = found_opp
    id_by_ref = {}
    for ch in cands:
        r = _cand_reference(ch)
        if r is not None:
            id_by_ref[str(r)] = ch.get("id")

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (5, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (2, str(o))

    picks = sorted(hand, key=rank_oid)[:max(1, n)]
    stype = spec.get("type") or "select"
    say(f"[{tag}] discarding to hand size via vi ({stype}): "
        f"{[obj_lname(state, o) for o in picks]}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": [id_by_ref[str(o)]
                                                               for o in picks
                                                               if str(o) in id_by_ref]}}},
                      tag)
    return True


async def play_a_land(c, state, pid, acts, tag, pref):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for name in pref:
        for o in hand_ids(state, pid):
            if obj_lname(state, o) != name.lower():
                continue
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


# ---------------- target selection / may-copy ------------------------------
def stack_entries(state):
    return state.get("stack") or []


def log_prompt_shape(tag, opp, purpose):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    shape = {"who": tag, "purpose": purpose,
             "iid": str(opp.get("interactionId"))[:16],
             "rtype": resp.get("type"),
             "n_choices": len(chs),
             "texts": [choice_text(ch)[:60] for ch in chs[:8]],
             "codes": [action_type_codes(ch) for ch in chs[:8]],
             "refs": [str(_cand_reference(ch)) for ch in chs[:8]]}
    wire("prompt_shape", shape)
    say(f"[{tag}] prompt ({purpose}): rtype={shape['rtype']} "
        f"n={shape['n_choices']} texts={shape['texts']} "
        f"refs={shape['refs']}")


async def answer_target_selection(c, st, state, pid, tag, purpose, pick_fn):
    """Answer a target-selection vi opportunity if one is present and the
    picker finds a match. Never blocks: returns False when no matching
    opportunity exists (the engine may auto-target)."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "tgt", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if rtype not in ("sequence", "schema", "exactChoices", "choose"):
            continue
        log_prompt_shape(tag, opp, purpose)
        pick = pick_fn(chs)
        if pick is None:
            say(f"[{tag}] target selection ({purpose}): no matching "
                f"candidate; leaving it")
            wire("target_no_match", {"who": tag, "purpose": purpose})
            SUBMITTED_OPPS.add(key)
            return False
        await answer_vi(c, opp, pick, tag)
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] answered {purpose}: {choice_text(pick)[:60]}")
        return True
    return False




def stack_targets(e):
    """Targets of a stack entry: top-level 'targets' if present, else the
    nested ability targets (kind.data.ability.targets), which is where
    protocol-142 Spell entries actually carry them (targets=null at top)."""
    t = e.get("targets")
    if t:
        return t
    try:
        return e["kind"]["data"]["ability"]["targets"]
    except Exception:
        return None

def find_schema_target_opp(st):
    """A schema response with candidates is the protocol-142 target-selection
    surface (cf. AGENTS.md #825); the waitingForKind.code may be 'sequence',
    not 'TargetSelection'."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        if data.get("candidates"):
            return opp
    return None

def cast_action_for(acts, state, name):
    for a in acts:
        if a.get("type") == "CastSpell":
            oid = a.get("data", {}).get("object_id")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
        if a.get("type") == "CastSpell":
            oid = a.get("_src_oid")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
    return None, None


def ints_in(obj, acc=None):
    if acc is None:
        acc = set()
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, int):
        acc.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            ints_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            ints_in(v, acc)
    return acc


def find_optional_copy_opp(st):
    """Find Ivy's may-copy OptionalEffectChoice opportunity: exactChoices
    with a decideOptionalEffect action code (proven scenario_301_01030.py
    shape)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in action_type_codes(ch):
                return opp
    return None


def pick_accept_choice(opp):
    """Pick the choice whose surface has role 'accept' and value 'true'."""
    chs = (opp.get("response", {}).get("data", {}) or {}).get("choices", [])
    for ch in chs:
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data", {}) or {}
            if d.get("role") == "accept" \
                    and str(d.get("value")).lower() == "true":
                return ch
    return None


async def ivy_copy_decision_tick(c, st, state):
    """Answer Ivy's 'you may copy' decideOptionalEffect with accept=true."""
    if not ST["bolt_cast"] or ST["copy_accepted"]:
        return False
    opp = find_optional_copy_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    key = ("P0", "ivy_copy", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    log_prompt_shape("P0", opp, "ivy-copy-may")
    pick = pick_accept_choice(opp)
    SUBMITTED_OPPS.add(key)
    ST["copy_decision_seen"] = True
    if pick is None:
        say("[P0] Ivy copy prompt WITHOUT accept choice; leaving unanswered")
        wire("ivy_copy_no_accept",
             {"kind": vi_kind_code(st)})
        return False
    ST["copy_accepted"] = True
    say("[P0] Ivy copy decision: ACCEPT")
    wire("ivy_copy_accept", {"kind": vi_kind_code(st),
                            "iid": str(iid)[:12]})
    await answer_vi(c, opp, pick, "P0")
    return True


async def copy_retarget_tick(c, st, state, tag):
    """If the engine raises CopyRetarget for the copy, record it and answer
    with Ivy (the correct target) so the flow completes."""
    if vi_kind_code(st) != "CopyRetarget":
        return False
    if not ST["copy_accepted"]:
        return False
    ST["copy_retarget_seen"] = True
    say(f"[{tag}] CopyRetarget prompt observed "
        f"(unexpected per acceptance criteria)")
    wire("copy_retarget_seen",
         {"kind": vi_kind_code(st), "stage": ST.get("stage")})
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "copy_retarget", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        log_prompt_shape(tag, opp, "copy-retarget")
        ivy = ST.get("ivy_oid")
        pick = None
        if ivy is not None:
            for ch in choices:
                r = _cand_reference(ch)
                if r is not None and str(r) == str(ivy):
                    pick = ch
                    break
        if pick is None and choices:
            pick = choices[0]
        if pick is None:
            continue
        SUBMITTED_OPPS.add(key)
        if ivy is not None and str(_cand_reference(pick)) == str(ivy):
            ST["copy_retarget_answered_ivy"] = True
        say(f"[{tag}] CopyRetarget: choosing ref={_cand_reference(pick)} "
            f"(ivy={ivy})")
        await answer_vi(c, opp, pick, tag)
        return True
    return False


# ---------------- per-seat ticks ------------------------------------------
async def p0_tick(c):
    """P0: mulligan, lands, Ivy, one Bear; accept Ivy's copy decision."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    rev = st.get("state_revision", -1)

    if await do_mulligan(c, acts, st, 0, "P0", p0_keep_pred):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, "P0"):
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

    # Ivy's may-copy owns any decideOptionalEffect prompt.
    if await ivy_copy_decision_tick(c, st, state):
        return True
    if await copy_retarget_tick(c, st, state, "P0"):
        return True

    # Never pass priority while Ivy's copy decision is pending for us.
    if ST["bolt_cast"] and not ST["copy_accepted"] \
            and find_optional_copy_opp(st) is not None:
        return True

    # Post-resolution: no more proactive play; keep priority moving only.
    if ST["post_exported"]:
        return False

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, "P0", [FOREST, ISLAND]):
            return True
        ivy_bf = bf_id(state, 0, IVY)
        if ivy_bf is None:
            # {G}{U}: need one untapped Forest and one untapped Island.
            if find_hand(state, 0, IVY) is not None \
                    and untapped_lands(state, 0, FOREST) \
                    and untapped_lands(state, 0, ISLAND):
                ca, _ = cast_action_for(acts, state, IVY)
                if ca and not acted("castivy", rev):
                    oid = int(find_hand(state, 0, IVY))
                    await submit_as_is(c, ca)
                    ST["cast_pending"] = (oid, rev, time.time())
                    say(f"[P0] casts Ivy (oid={oid})")
                    wire("ivy_cast", {"turn": state.get("turn_number"),
                                      "oid": oid})
                    return True
        elif bf_id(state, 0, BEAR) is None:
            # exactly one Bear: {1}{G}
            if find_hand(state, 0, BEAR) is not None \
                    and untapped_land_count(state, 0) >= 2 \
                    and untapped_lands(state, 0, FOREST):
                ca, _ = cast_action_for(acts, state, BEAR)
                if ca and not acted("castbear", rev):
                    oid = int(find_hand(state, 0, BEAR))
                    await submit_as_is(c, ca)
                    ST["cast_pending"] = (oid, rev, time.time())
                    say(f"[P0] casts Bear (oid={oid})")
                    wire("bear_cast", {"turn": state.get("turn_number"),
                                       "oid": oid})
                    return True
    return False


async def p1_tick(c):
    """P1: mulligan, lands; cast one Bolt at P0's Bear once the fixture is
    ready; answer the Bolt target selection via vi."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    rev = st.get("state_revision", -1)

    if await do_mulligan(c, acts, st, 1, "P1", p1_keep_pred):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, "P1"):
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

    # Bolt target selection: choose P0's Bear. The target prompt is a
    # schema opportunity with candidates; waitingForKind.code may read
    # "sequence" rather than "TargetSelection" (proven by the 20261016-0731
    # stall dump), so gate on the opportunity shape, not the kind code.
    if ST["bolt_cast"] and not ST["bolt_target_chosen"]:
        bear = ST.get("bear_oid")
        if bear is None:
            bear = bf_id(state, 0, BEAR)
            if bear is not None:
                ST["bear_oid"] = bear
        if find_schema_target_opp(st) is not None:
            done = await answer_target_selection(
                c, st, state, 1, "P1", "bolt-target",
                lambda chs: _pick_creature_target(chs, bear))
            if done:
                ST["bolt_target_chosen"] = True
                ST["bolt_target_oid"] = bear
                return True
            # prompt present but no matching candidate yet; hold priority
            return True
        # prompt vanished (engine auto-targeted or answered); do not wedge
        ST["bolt_target_chosen"] = True
        wire("bolt_target_prompt_gone", {"bear": bear})
        return True

    if await copy_retarget_tick(c, st, state, "P1"):
        return True

    # Post-resolution: no more proactive play; keep priority moving only.
    if ST["post_exported"]:
        return False

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, "P1", [MOUNTAIN]):
            return True
        if ST["pre_exported"] and not ST["bolt_cast"] \
                and ST["ivy_oid"] is not None \
                and bf_id(state, 0, BEAR) is not None \
                and find_hand(state, 1, BOLT) is not None \
                and untapped_land_count(state, 1) >= 1:
            ca, _ = cast_action_for(acts, state, BOLT)
            if ca and not acted("castbolt", rev):
                oid = int(find_hand(state, 1, BOLT))
                # CastSpell submitted EXACTLY as advertised; targets are
                # chosen via the engine's vi TargetSelection afterwards
                # (CR 601.2c).
                await submit_as_is(c, ca)
                ST["bolt_cast"] = True
                ST["bolt_oid"] = oid
                ST["cast_pending"] = (oid, rev, time.time())
                ST["stage"] = "BOLT_CAST"
                say(f"[P1] casts Bolt (oid={oid}); awaiting target selection")
                wire("bolt_cast", {"turn": state.get("turn_number"),
                                   "oid": oid})
                return True
    return False


def _pick_creature_target(chs, want_oid):
    if want_oid is not None:
        for ch in chs:
            r = _cand_reference(ch)
            if r is not None and str(r) == str(want_oid):
                return ch
    for ch in chs:
        if BEAR.lower() in choice_text(ch).lower():
            return ch
    return chs[0] if len(chs) == 1 else None


# ---------------- observation ------------------------------------------------
async def observe(state):
    turn = state.get("turn_number") or 0

    if ST["ivy_oid"] is None:
        iv = bf_id(state, 0, IVY)
        if iv is not None:
            ST["ivy_oid"] = iv
            say(f"Ivy on BF oid={iv}")
            wire("ivy_bf", {"oid": iv})
    if ST["bear_oid"] is None:
        b = bf_id(state, 0, BEAR)
        if b is not None:
            ST["bear_oid"] = b
            say(f"Bear on BF oid={b}")
            wire("bear_bf", {"oid": b})

    # pre.json: fixture ready -- Ivy + one Bear on P0 BF; P1 to act in
    # main phase holding Bolt with an untapped Mountain. P1's hand is only
    # visible from P1's own view.
    p1st = (C1.latest or {}).get("state") if C1 is not None else None
    if (p1st is not None and not ST["pre_exported"]
            and ST["ivy_oid"] is not None
            and ST["bear_oid"] is not None
            and BOLT.lower() in hand_lnames(p1st, 1)
            and untapped_lands(p1st, 1, MOUNTAIN)
            and p1st.get("phase") in ("PreCombatMain", "PostCombatMain")
            and p1st.get("active_player") == 1
            and p1st.get("priority_player") == 1
            and not (p1st.get("stack") or [])):
        env = await export_now("pre.json")
        if env is not None:
            ST["pre_exported"] = True
            ST["stage"] = "FIXTURE"
            n = len(untapped_lands(p1st, 1, MOUNTAIN))
            ST["notes"].append(
                f"pre: turn {p1st.get('turn_number')} ivy_oid={ST['ivy_oid']} "
                f"bear_oid={ST['bear_oid']} P1 hand has Bolt, "
                f"{n} untapped Mountain(s)")
            say("fixture ready; pre.json exported")

    # Copy window: new stack entries after the accept (robust to the copy
    # not carrying the original's name).
    if ST["bolt_cast"]:
        cur_ids = {e.get("id") for e in stack_entries(state)
                   if e.get("id") is not None}
        if not ST["copy_accepted"]:
            ST["seen_stack_ids"] |= cur_ids
        elif not ST["saw_two_bolts"]:
            new_ids = cur_ids - ST["seen_stack_ids"]
            ST["seen_stack_ids"] |= cur_ids
            for e in stack_entries(state):
                if e.get("id") not in new_ids:
                    continue
                kind = e.get("kind") or {}
                ktype = kind.get("type") if isinstance(kind, dict) else kind
                if ktype in ("Spell", "CastSpell", "SpellCopy"):
                    ST["saw_two_bolts"] = True
                    ST["copy_stack_id"] = e.get("id")
                    snap = {"id": e.get("id"), "name": e.get("name"),
                            "card_name": e.get("card_name"),
                            "controller": e.get("controller"),
                            "targets": stack_targets(e), "kind": e.get("kind")}
                    ST["copy_entry"] = snap
                    say(f"COPY DETECTED: new stack entry id={e.get('id')} "
                        f"kind={ktype} controller={e.get('controller')} "
                        f"targets={json.dumps(e.get('targets'), default=str)[:200]}")
                    wire("copy_detected", {"entry": snap})
                    break
        # Fallback: two name-matched Bolt spells on the stack.
        bolts = [e for e in stack_entries(state)
                 if "lightning bolt" in
                 str(e.get("name") or e.get("card_name") or "").lower()]
        if len(bolts) >= 2 and not ST["saw_two_bolts"]:
            ST["saw_two_bolts"] = True
            for e in bolts:
                snap = {"id": e.get("id"), "name": e.get("name"),
                        "card_name": e.get("card_name"),
                        "controller": e.get("controller"),
                        "targets": stack_targets(e), "kind": e.get("kind")}
                if str(e.get("controller")) == "0":
                    ST["copy_entry"] = snap
                elif str(e.get("controller")) == "1":
                    ST["orig_entry"] = snap
            say(f"COPY WINDOW: {len(bolts)} bolt spells on stack; "
                f"copy_entry={json.dumps(ST['copy_entry'], default=str)[:200]}")
            wire("copy_window", {"n": len(bolts), "copy": ST["copy_entry"],
                                 "orig": ST["orig_entry"]})
        if ST["saw_two_bolts"] and not ST["mid_exported"]:
            env = await export_now("mid.json")
            if env is not None:
                ST["mid_exported"] = True
                ST["stage"] = "COPY_WINDOW"
                ST["notes"].append(
                    f"mid: copy targets raw="
                    f"{json.dumps((ST.get('copy_entry') or {}).get('targets'), default=str)[:160]}")

    # Resolution: no Bolt spells on the stack after the copy window.
    bolts_now = [e for e in stack_entries(state)
                 if "lightning bolt" in
                 str(e.get("name") or e.get("card_name") or "").lower()]
    if ST["saw_two_bolts"] and not bolts_now:
        ST["quiescent_ticks"] += 1
    else:
        ST["quiescent_ticks"] = 0
    if (ST["saw_two_bolts"] and ST["quiescent_ticks"] >= 3
            and not ST["post_exported"]):
        env = await export_now("post.json")
        if env is not None:
            ST["post_exported"] = True
            ST["stage"] = "RESOLVED"
            ST["resolve_turn"] = turn
            ST["resolve_wall"] = time.time()
            ST["post_turn"] = turn
            post = load_state("post.json")
            ivy_alive = get_obj(post, ST["ivy_oid"]).get("zone") == \
                "Battlefield"
            bear_zone = get_obj(post, ST["bolt_target_oid"]).get("zone")
            ST["notes"].append(
                f"post: turn {turn} ivy_alive={ivy_alive} "
                f"targeted_bear_zone={bear_zone} "
                f"life={life(post, 0)}/{life(post, 1)}")
            say(f"RESOLVED: ivy_alive={ivy_alive} bear_zone={bear_zone}")
        return

    # after post: play a few more turns for cleanup, then stop
    if ST["stage"] == "RESOLVED" and ST["post_turn"] is not None:
        if turn >= ST["post_turn"] + 3:
            ST["done_reason"] = f"cleanup window closed turn={turn}"
            say("DONE: " + ST["done_reason"])
            ST["stop"] = True
            return
    _vi0 = (C0.latest or {}).get("viewer_interaction") or {}
    if ((_vi0.get("waitingForKind") or {}).get("code") or "") == "gameOver" \
            and not ST["game_over"]:
        say("game over; exporting post.json before the session expires")
        ST["game_over"] = True
        if not os.path.exists(f"{EVDIR}/post.json"):
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
        ST["stop"] = True


# ---------------- tick + main loop ----------------------------------------
def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage"),
                                     "t": time.time()})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def tick(c, pid):
    st = c.latest
    if not st or ST["stop"]:
        return False
    state = st.get("state")
    if state is None:
        return False
    acts = merged_actions(st)
    drain_rejections(c)
    kind = vi_kind_code(st)
    if kind and (not WF_SEEN or WF_SEEN[-1] != kind):
        WF_SEEN.append(kind)
        wire("waiting_for_kind", {"kind": kind, "stage": ST.get("stage")})
        say(f"[{c.name}] vi waitingForKind: {kind}")
    for q in (0, 1):
        lv = life(state, q)
        if lv is not None and lv != ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {state.get('turn_number')} phase {state.get('phase')})")
            wire("life_change", {"player": q, "from": ST["life"][q], "to": lv,
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
            ST["life"][q] = lv
    try:
        if pid == 0:
            did = await p0_tick(c)
        else:
            did = await p1_tick(c)
    except Exception as e:
        say(f"tick error [{c.name}]: {e}")
        wire("tick_error", {"who": c.name, "error": str(e)[:300]})
        return False
    if did:
        return True
    # never pass priority while a cast we submitted is still in flight
    # (the engine must process it first, else our pass races the cast)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    cp = ST.get("cast_pending")
    if cp:
        coid, crev, ct0 = cp
        cur_rev = st.get("state_revision", -1)
        o = get_obj(state, coid)
        if cur_rev <= crev and o.get("zone") == "Hand" \
                and time.time() - ct0 < 30:
            return False
        ST["cast_pending"] = None
    if my_priority(acts):
        await pass_priority(c, st, acts)
        return True
    return False


# ---------------- finalize -------------------------------------------------
async def finalize():
    if ST.get("finalized"):
        return None, {}
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_7168_01100.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    if "post.json" not in ST["exports"]:
        await export_now("post.json")

    post = load_state("post.json")
    ass, notes = {}, []

    def note(m):
        notes.append(m)
        say("NOTE:", m)

    # ---- A1: setup ----
    if (ST["bolt_cast"] and ST["bolt_target_chosen"]
            and ST["ivy_oid"] is not None and ST["pre_exported"]):
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1: P1 cast Bolt (oid={ST['bolt_oid']}) targeting P0's Bear "
            f"(oid={ST['bolt_target_oid']}, answered via vi); Ivy "
            f"(oid={ST['ivy_oid']}) + one Bear on P0 BF; pre.json exported.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1 failed: bolt_cast={ST['bolt_cast']} "
                     f"bolt_target_chosen={ST['bolt_target_chosen']} "
                     f"ivy_oid={ST['ivy_oid']} "
                     f"pre_exported={ST['pre_exported']}")

    # ---- A2: trigger offered ----
    if ST["copy_decision_seen"]:
        ass["A2_trigger_offered"] = "passed"
        notes.append("A2: Ivy's optional-copy decideOptionalEffect raised "
                     "for P0 and answered accept=true.")
    else:
        ass["A2_trigger_offered"] = "failed"
        notes.append("A2 failed: no Ivy copy decision observed.")

    # ---- A3: copy created ----
    if ST["saw_two_bolts"]:
        ass["A3_copy_created"] = "passed"
        notes.append(f"A3: second Bolt spell (the copy) observed on the "
                     f"stack: {json.dumps(ST.get('copy_entry'), default=str)[:200]}")
    else:
        ass["A3_copy_created"] = "failed"
        notes.append("A3 failed: no copy stack entry observed after accept.")

    # ---- A4: copy targets Ivy ----
    copy_targets_ivy = False
    ce = ST.get("copy_entry")
    if ce is not None and ST["ivy_oid"] is not None:
        tgt_ints = ints_in(ce.get("targets"))
        copy_targets_ivy = ST["ivy_oid"] in tgt_ints
        note(f"A4: copy entry targets ints include ivy({ST['ivy_oid']})="
             f"{copy_targets_ivy}; bear({ST['bolt_target_oid']})="
             f"{ST['bolt_target_oid'] in tgt_ints}")
    if copy_targets_ivy:
        ass["A4_copy_targets_ivy"] = "passed"
        notes.append("A4: the copy's targets include Ivy.")
    else:
        ass["A4_copy_targets_ivy"] = "failed"
        notes.append("A4 FAILED: the copy's targets do not include Ivy "
                     "(reported bug).")

    # ---- A5: no CopyRetarget prompt ----
    if ST["copy_retarget_seen"]:
        ass["A5_no_retarget_prompt"] = "failed"
        notes.append(f"A5: a CopyRetarget prompt WAS raised for the copy "
                     f"(answered_ivy={ST['copy_retarget_answered_ivy']}).")
    else:
        ass["A5_no_retarget_prompt"] = "passed"
        notes.append("A5: no CopyRetarget prompt was raised for the copy.")

    # ---- A6: outcome ----
    if (post is not None and ST["bolt_target_oid"] is not None
            and ST["ivy_oid"] is not None):
        ivy_alive = get_obj(post, ST["ivy_oid"]).get("zone") == "Battlefield"
        bear_zone = get_obj(post, ST["bolt_target_oid"]).get("zone")
        bear_dead = bear_zone != "Battlefield"
        if ivy_alive and bear_dead:
            ass["A6_outcome"] = "passed"
            notes.append(f"A6: post: Ivy alive on BF (zone=Battlefield); "
                         f"targeted Bear dead (zone={bear_zone}) -- the "
                         f"copy did not hit Ivy.")
        else:
            ass["A6_outcome"] = "failed"
            notes.append(f"A6 FAILED: post ivy_alive={ivy_alive} "
                         f"bear_zone={bear_zone}")
    else:
        ass["A6_outcome"] = "not-run"
        notes.append("A6 not-run: post.json missing.")

    # ---- A7: cleanup ----
    if post is not None:
        if not (post.get("stack") or []):
            ass["A7_cleanup"] = "passed"
            notes.append("A7: stack empty; game advanced; post.json "
                         "exported.")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 FAILED: stack not empty at post.")
    else:
        ass["A7_cleanup"] = "failed"
        notes.append("A7 failed: post.json missing.")

    for k, v in ass.items():
        say(f"{k}: {v}")

    # ---- verdict ----
    if ass["A1_setup_ok"] == "failed" or ass["A2_trigger_offered"] == "failed":
        verdict = "blocked"
    elif ass["A3_copy_created"] == "passed" \
            and ass["A4_copy_targets_ivy"] == "failed":
        verdict = "reproduced"
    elif ass["A1_setup_ok"] == "passed" \
            and ass["A2_trigger_offered"] == "passed" \
            and ass["A3_copy_created"] == "passed" \
            and ass["A4_copy_targets_ivy"] == "passed":
        verdict = "not-reproduced"
        if ass["A5_no_retarget_prompt"] == "failed":
            notes.append("limitation: the copy targets Ivy only after a "
                         "CopyRetarget prompt (engine now offers the "
                         "retarget that was previously unsupported).")
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    run = {
        "issue": ISSUE,
        "title": "Ivy, gleeful spellthief -- correctly copies the spell but "
                 "doesn't target itself with the copy",
        "run_id": RUN_ID,
        "validated_at": "2026-10-16",
        "validated_version": "v0.110.0",
        "server": SERVER_IDENTITY,
        "server_hello_observed": ST.get("server_hello"),
        "protocol_version": 142,
        "scope": "Native engine, two human-client seats; P1 casts Lightning "
                 "Bolt targeting P0's Grizzly Bears; P0 accepts Ivy's "
                 "may-copy; the copy's targets and resolution outcome are "
                 "asserted explicitly. Re-validation of run 20260915-7168d "
                 "(v0.83.0/protocol 70, reproduced) on the v0.110.0 pin. "
                 "The Aura-copy clause is untestable with this fixture "
                 "(not-run; parse evidence captured).",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {v}" for k, v in ass.items()),
        "parse_chain": ST.get("parse_chain"),
        "driver_state": {
            "ivy_oid": ST["ivy_oid"],
            "bear_oid": ST["bear_oid"],
            "bolt_oid": ST["bolt_oid"],
            "bolt_stack_id": ST.get("bolt_stack_id"),
            "bolt_target_oid": ST.get("bolt_target_oid"),
            "bolt_target_chosen": ST["bolt_target_chosen"],
            "copy_decision_seen": ST["copy_decision_seen"],
            "copy_accepted": ST["copy_accepted"],
            "copy_retarget_seen": ST["copy_retarget_seen"],
            "copy_retarget_answered_ivy": ST["copy_retarget_answered_ivy"],
            "saw_two_bolts": ST["saw_two_bolts"],
            "copy_entry": ST.get("copy_entry"),
            "orig_entry": ST.get("orig_entry"),
            "copy_stack_id": ST["copy_stack_id"],
            "resolve_turn": ST["resolve_turn"],
            "turns_seen": sorted(ST["turns_seen"]),
            "rejections": ST["rejections"][:10],
            "wf_kinds_seen": WF_SEEN,
            "unexpected": ST["unexpected"],
        },
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The Aura-copy clause (\"a copy of an Aura spell becomes a "
            "token\") is not exercised; it is part of the same unsupported "
            "retarget child in the pinned parse.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Backfill-owned isolated single-user server on 127.0.0.1:9374; "
            "binary identity re-verified at run start (sha256 matches the "
            "signed v0.110.0 manifest).",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
        "done_reason": ST.get("done_reason"),
        "prior_run": {
            "run_id": "20260915-7168d",
            "validated_version": "v0.83.0",
            "verdict": "reproduced",
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # render summary.png via the standalone renderer (reads run.json +
    # saved states; must run before manifest)
    try:
        r = __import__("subprocess").run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7168.py", EVDIR],
            capture_output=True, text=True, timeout=120)
        say(r.stdout.strip())
        if r.returncode != 0:
            say("renderer failed: " + r.stderr[-500:])
            wire("renderer_failed", {"stderr": r.stderr[-500:]})
    except Exception as e:
        say(f"renderer error: {e}")
        wire("renderer_error", {"error": str(e)[:200]})

    # manifest LAST (after all logging is done) - no say()/wire() calls
    # may happen after this point or the hashed logs will drift.
    say(f"writing manifest; done: {ST.get('done_reason')}")
    RUNLOG.flush()
    WIRE.flush()
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            f.write(f"{h}  {fn}\n")
    RUNLOG.flush()
    WIRE.flush()
    return verdict, ass


# ---------------- main -----------------------------------------------------
async def main():
    reset()
    global C0, C1
    await verify_server_hello()
    parse_ivy()

    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "run_id": RUN_ID})

    t0 = time.time()
    last_rev_seen = {0: -1, 1: -1}
    last_rev_time = time.time()
    stall_dumped = False
    try:
        while time.time() - t0 < TIMEOUT and not ST["stop"]:
            await tick(C0, 0)
            await tick(C1, 1)
            st = C0.latest
            if st:
                state = st["state"]
                turn = state.get("turn_number") or 0
                if turn >= 1:
                    ST["game_started"] = True
                    ST["turns_seen"].add(turn)
                if ST["stage"] == "SETUP" and turn > SETUP_TURN_CAP:
                    say(f"SETUP_TURN_CAP {SETUP_TURN_CAP} hit; finishing")
                    ST["done_reason"] = \
                        f"setup turn cap {SETUP_TURN_CAP} exceeded"
                    break
                await observe(state)
            # stall watchdog: no revision progress on either client for 90s
            cur0 = (C0.latest or {}).get("state_revision", -1)
            cur1 = (C1.latest or {}).get("state_revision", -1)
            if cur0 != last_rev_seen[0] or cur1 != last_rev_seen[1]:
                last_rev_seen = {0: cur0, 1: cur1}
                last_rev_time = time.time()
                stall_dumped = False
            elif ST["game_started"] and time.time() - last_rev_time > 90 \
                    and not stall_dumped:
                stall_dumped = True
                ST["stall_observed"] = True
                say("STALL WATCHDOG: no revision progress for 90s; "
                    "dumping client views")
                wire("stall_watchdog", {"rev0": cur0, "rev1": cur1})
                for cc, tag in ((C0, "P0"), (C1, "P1")):
                    sst = cc.latest or {}
                    sstate = (sst.get("state") or {})
                    aats = sorted(set(a.get("type")
                                       for a in merged_actions(sst)))
                    vvi = sst.get("viewer_interaction") or {}
                    opps = []
                    for o in (vvi.get("opportunities") or [])[:4]:
                        rr = o.get("response", {}) or {}
                        cchs = (rr.get("data") or {}).get("choices") or \
                            (rr.get("data") or {}).get("candidates") or []
                        opps.append({
                            "iid": str(o.get("interactionId"))[:16],
                            "rtype": rr.get("type"),
                            "choices": [
                                {"id": str(ch.get("id"))[:16],
                                 "action_codes": action_type_codes(ch),
                                 "text": choice_text(ch)[:60]}
                                for ch in cchs]})
                    dump = {
                        "rev": sst.get("state_revision"),
                        "turn": sstate.get("turn_number"),
                        "phase": sstate.get("phase"),
                        "active": sstate.get("active_player"),
                        "stack_n": len(sstate.get("stack") or []),
                        "atypes": aats,
                        "vi_kind": (vvi.get("waitingForKind") or {})
                        .get("code"),
                        "vi_canSubmit": vvi.get("canSubmit"),
                        "vi_opps": opps,
                    }
                    say(f"[{tag}] STALLDUMP " + json.dumps(dump)[:1500])
                    wire("stall_dump", {"who": tag, "dump": dump})
                ST["stop"] = True
            await asyncio.sleep(0.25)
    except Exception as e:
        say(f"main loop error: {e}")
        wire("main_error", {"error": str(e)[:300]})
    finally:
        if not ST.get("finalized"):
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"pre-close post export failed: {e}")
                wire("post_export_failed", {"error": str(e)[:200]})
        try:
            await C0.close()
        except Exception:
            pass
        try:
            await C1.close()
        except Exception:
            pass
    return await finalize()


if __name__ == "__main__":
    verdict, ass = asyncio.run(main())
    print(f"FINAL verdict={verdict} "
          + " ".join(f"{k}={v}" for k, v in ass.items()))
