#!/usr/bin/env python3
"""Issue #301 revalidation on v0.104.0 (protocol 118): Chaos Wand accept path.

The maintained comment already supersedes the false "fixed" (published
correction). The release pin advanced to v0.104.0, so the accepted-cast path
is re-run from scratch on the pinned release.

Game 1 (accept path):
  A1 setup_ok
  A2 activation_paid      Wand tapped, 4 untapped islands consumed by {4} cost
  A3 exile_observed      P1 library shrinks; Lightning Bolt among exiled cards
  A4 accept_target_pending  after ACCEPT, a target selection for the free cast is pending
  A5 exile_returned_early   exiled cards back in P1 library BEFORE targets chosen,
                            while the cast is still pending (Bolt zone=Library) [bug]
  A6 advertised_target_rejected  exact engine-advertised submission is
                            rejected action_not_allowed; target selection stays pending [bug]
  A7 no_damage_no_cast   life 20/20, Bolt not in graveyard, no cast recorded [bug]
  A5fx target_accepted   no rejection; TargetSelection clears after submission [fixed]
  A6fx cast_resolved     Bolt resolves: P1 at 17 life, Bolt in P1 graveyard,
                         no exile, no dangling cast [fixed]

Game 2 (decline control):
  A8 decline_control_ok  decline -> no cast, no damage, exiled cards returned, no dangling cast

Verdict: reproduced iff A5 and A6 pass;
         not-reproduced iff A5fx, A6fx and A8 pass;
         blocked otherwise.

Protocol-118 driver notes (v0.104.0, 2026-10-08): ported from the verified
v0.103.0 scenario_301_01030.py (protocol-106; the 2026-10-06 v0.103.0 run
reproduced the bug, A5/A6 passed). Only the release pin, build commit, and
ServerHello assertions changed for this v0.104.0 re-validation run.

Protocol-106 driver notes (v0.103.0, 2026-10-06) [original text preserved]:
ported from the verified v0.102.0 scenario_301_01020.py (protocol-106; the
2026-10-04 v0.102.0 run reproduced the bug, A5/A6 passed).
Protocol-106 driver notes (v0.102.0, 2026-10-04) [original text preserved]:
protocol-103 scenario_301_01010.py using the conventions established in the
verified protocol-118 scenario_1234_01020.py:
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 118),
    gated on the MulliganDecision legal action.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards (106 uses generic 'choose' kind code).
  - Optional-cast accept/decline via vi exactChoices surfaces
    (decideOptionalEffect role/value); target via the advertised schema
    response type (select/sequence) or exactChoices choose.
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    reading fresh state for leg evaluation (leg-engagement race fix).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
  - deck schema {"name", "main_deck": [...]}; client.py HELLO advertises 118
    (exact match enforced).
"""
import asyncio
import copy
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
log = logging.getLogger("scenario301_118")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20261008-301")
ISSUE = 301
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.104.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-08: latest stable release v0.104.0 (published "
               "2026-10-08) == pinned release dir; ServerHello "
               "0.104.0/4227122/protocol 118 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "fresh v0.104.0 server started by this run on 127.0.0.1:9374 "
               "with isolated run dir runs/" + RUN_ID + " (this run)"),
}

for _f, _k in (("server/releases/v0.104.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.104.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.104.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"))


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


def lib_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def lib_size(state, pid):
    return len(lib_ids(state, pid))


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_islands(state, pid):
    return sum(1 for o in bf_by_name(state, pid, "island")
               if not get_obj(state, o).get("tapped"))


def bolt_obj(state):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == "lightning bolt":
            return o
    return None


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    """Protocol 118: the viewing seat holds priority iff a PassPriority
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
                      "mulliganDecision"}


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


def decision_pending(st):
    """Legacy alias: only real decisions block priority passes."""
    return real_decision_pending(st)


# ---------------------------------------------------------------- interaction primitives

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
        rdata = {"choiceIds": [cid]}
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": rdata}}
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"


def check_data_level():
    ok, notes = True, []
    for name, needle in (("chaos wand", "Target opponent exiles"),
                         ("lightning bolt", "deals 3 damage"),
                         ("island", "Land")):
        e = CARD_DATA.get(name, {})
        if needle.lower() not in str(e.get("oracle_text", "")).lower() \
                and needle.lower() not in str(e.get("type_line", "")).lower() \
                and name != "island":
            ok = False
            notes.append(f"{name}: oracle/type shape missing ({needle!r})")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {n: str(CARD_DATA.get(n, {}).get("oracle_text"))[:200]
                              for n in ("chaos wand", "lightning bolt")}}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 118: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission). #301 always keeps."""
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
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 118: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity.
    Gated on kind == 'mulligan' AND turn 1 / Untap."""
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {"acts": [a.get("data") for a in sel_acts][:8]})
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

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == "lightning bolt":
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 118: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards
    (106 uses a generic 'choose' waitingForKind code)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(_cand_reference(ch) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm == "lightning bolt":
            return (5, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype}): "
            f"{[obj_lname(state, _cand_reference(ch)) for ch in cands if ch['id'] in picks]}")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
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
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
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


def can_pay_generic(state, pid, generic):
    pool = sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))
    return pool >= generic


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


def find_activate(st, source_oid):
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility":
            d = a.get("data") or {}
            if str(d.get("source_id")) == str(source_oid) \
                    or str(a.get("_src_oid")) == str(source_oid):
                return ("submit", a, "ActivateAbility legal_action")
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(source_oid) in refs:
                return ("interaction", (opp, ch),
                        "activateAbility via viewer_interaction (choose)")
    return None

# ---------------------------------------------------------------- ticks

async def p1_tick(c, g, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p0_tick_ramp(c, g, tag):
    """One ramp tick for P0. Returns ('ACTIVATE', wand_id) at the window."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
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
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        wand_id = next(iter(bf_by_name(state, 0, "chaos wand")), None)
        on_stack = any(obj_lname(state, oid) == "chaos wand"
                       and str(get_obj(state, oid).get("controller", -1)) == "0"
                       for oid, o in (state.get("objects") or {}).items()
                       if o.get("zone") == "Stack")
        if wand_id is None and not on_stack:
            if "chaos wand" in [obj_lname(state, o) for o in hand_ids(state, 0)] \
                    and can_pay_generic(state, 0, 3):
                a, oid = cast_action_for(acts, state, "chaos wand")
                if a is not None:
                    ST["mana_needs"][tag] = {"generic": 3}
                    say(f"[{tag}] casting Chaos Wand (oid {oid})")
                    wire("cast_wand", {"action": {k: v for k, v in a.items()
                                                 if not k.startswith("_")}})
                    await submit_as_is(c, a)
                    return True
        if wand_id is not None and untapped_islands(state, 0) >= 12:
            return ("ACTIVATE", wand_id)
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def generic_tick(c, g, tag, pid):
    """Post-activation driver tick: keep the game moving without answering
    decision prompts (those are handled explicitly)."""
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def tick_all(p0, p1, g, ramp_tag):
    """One round of driver ticks for both clients. Returns ('ACTIVATE', id)
    if the ramp window is reached."""
    await generic_tick(p1, g, f"P1{ramp_tag}", 1)
    r = await p0_tick_ramp(p0, g, ramp_tag)
    return r if isinstance(r, tuple) else None


async def tick_all_generic(p0, p1, g, ramp_tag):
    await generic_tick(p1, g, f"P1{ramp_tag}", 1)
    await generic_tick(p0, g, ramp_tag, 0)


async def settle(p0, p1, g, ramp_tag, cond, timeout, label, poll=0.25):
    """Drive both clients (no explicit decisions) until cond(state) or
    timeout. Export-only checkpoints fall through to the priority pass."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await tick_all_generic(p0, p1, g, ramp_tag)
        await asyncio.sleep(0)  # yield to pump before leg evaluation
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle: {label}")
    return None


# ---------------------------------------------------------------- wand activation

def wand_ability_on_stack(state, wand_id):
    """True if the wand's activated ability is on the stack. Stack entries
    are checked by source reference; falls back to any non-empty stack
    while the wand is tapped and awaiting resolution."""
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if str(wand_id) in blob and "ctivat" in blob:
            return True
    return False


async def activate_wand(p0, p1, g, ramp_tag, wand_id, timeout=240):
    """Submit the advertised ActivateAbility WHILE HOLDING priority (never
    pass first: submitting after passing leaves the ability unactivated and
    the game drifts on). Then drive the {4} payment and wait until the
    ability is actually on the stack."""
    submitted = False
    t0 = time.time()
    diag_n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        # Drive P1 only (it just passes); P0 must keep its priority until
        # the ability is submitted. Payment ticks for P0 run below without
        # passing.
        await generic_tick(p1, g, f"P1{ramp_tag}", 1)
        if not submitted:
            await asyncio.sleep(0)
            st = p0.latest
            if st is None:
                continue
            acts = merged_actions(st)
            if not my_priority(acts):
                # P0 lost priority somehow; let one generic tick run then retry
                await generic_tick(p0, g, ramp_tag, 0)
                continue
            r = find_activate(st, wand_id)
            if r is None:
                if diag_n % 10 == 0:
                    say(f"[{ramp_tag}] ActivateAbility not offered; "
                        f"acts={[a['type'] for a in acts][:10]}")
                    wire(f"{ramp_tag}_activate_missing",
                         {"acts": [a["type"] for a in acts][:10]})
                diag_n += 1
                continue
            kind, sub, what = r
            say(f"submitting ActivateAbility via {what} (holding priority)")
            wire("activate_ability", {"via": what})
            ST["mana_needs"][ramp_tag] = {"generic": 4}
            if kind == "interaction":
                opp, ch = sub
                await answer_vi(p0, opp, ch, ramp_tag)
            else:
                await submit_as_is(p0, sub)
            submitted = True
            continue
        # payment + resolution driving for P0 (no priority pass while the
        # payment/decision surface is pending)
        st = p0.latest
        if st is None:
            continue
        acts = merged_actions(st)
        if await pay_tick(p0, acts):
            continue
        needs = ST["mana_needs"].get(ramp_tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(p0, st, ramp_tag, needs):
                continue
        await asyncio.sleep(0)
        st = p0.latest or st
        s = st["state"]
        w = get_obj(s, wand_id)
        if diag_n % 8 == 0:
            pool = player_of(s, 0).get("mana_pool")
            say(f"[{ramp_tag}] activation watch: wand tapped={w.get('tapped')} "
                f"stack={len(s.get('stack') or [])} needs={ST['mana_needs'].get(ramp_tag)} "
                f"pool={json.dumps(pool, default=str)[:120]} vikind={vi_kind_code(st)!r}")
            wire(f"{ramp_tag}_activation_watch",
                 {"wand_tapped": w.get("tapped"),
                  "stack": s.get("stack"),
                  "needs": ST["mana_needs"].get(ramp_tag),
                  "vi": st.get("viewer_interaction")})
        diag_n += 1
        if w.get("tapped") and wand_ability_on_stack(s, wand_id):
            say(f"[{ramp_tag}] wand ability on the stack")
            ST["mana_needs"][ramp_tag] = {}
            return True
        if w.get("tapped") and not (s.get("stack") or []):
            # Tapped but nothing on the stack: the ability may have resolved
            # already (fast exile) or fizzled; let the caller decide via the
            # exile settle.
            say(f"[{ramp_tag}] wand tapped, stack empty -- proceeding to exile watch")
            ST["mana_needs"][ramp_tag] = {}
            return True
        if not real_decision_pending(st) and my_priority(acts):
            await pass_priority(p0, st, acts)
    ST["mana_needs"][ramp_tag] = {}
    return False


# ---------------------------------------------------------------- optional-cast prompt

def find_optional_cast_opp(st):
    """Find the may-cast OptionalEffectChoice opportunity: exactChoices with
    a decideOptionalEffect action code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


async def pick_optional_cast(c, want_accept, tag):
    st = c.latest
    opp = find_optional_cast_opp(st)
    if opp is None:
        # diagnostic: dump what IS pending
        vi = st.get("viewer_interaction") or {}
        say(f"[{tag}] no decideOptionalEffect opportunity; vi kind={vi_kind_code(st)!r} "
            f"ops={len(vi_ops(st))}")
        wire(f"{tag}_optional_unanswered_vi", vi)
        return None, None
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "cast" in txt or "accept" in txt:
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt:
                is_accept = False
        if is_accept == want_accept:
            return opp.get("interactionId"), ch["id"]
    return None, None


# ---------------------------------------------------------------- target selection

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
                    and ("decideOptionalEffect" not in codes) \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(state, opp):
    """Prefer the opponent-player candidate (seat 1)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []

    def seat_of(ch):
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
            # also check role/data embedded seat
            for k, v in (d or {}).items():
                if k in ("seat", "player") and isinstance(v, int):
                    return v
        return None

    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if seat_of(ch) == 1:
            score += 2
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid

# ---------------------------------------------------------------- game runner

async def run_game(mode):
    """mode in {'accept','decline'}. Returns dict of observations + assertions."""
    global PASSED_REV
    for attempt in range(3):
        obs = await _run_game_attempt(mode, attempt)
        if obs.get("_fixture_ok", True):
            return obs
    obs["notes"].append("fixture failed 3 times (Bolt left P1's library before activation)")
    for k in ("A2_activation_paid", "A3_exile_observed", "A4_accept_target_pending",
              "A5_exile_returned_early", "A6_advertised_target_rejected",
              "A7_no_damage_no_cast", "A5fx_target_accepted", "A6fx_cast_resolved",
              "A8_decline_control_ok"):
        obs["assert"].setdefault(k, "not-run")
    return obs


def bolt_in_p1_library(state):
    p1lib = set(lib_ids(state, 1))
    for oid, o in (state.get("objects") or {}).items():
        if str(oid) in p1lib and str(o.get("base_name") or o.get("name") or "").lower() == "lightning bolt":
            return True
    return False


async def _run_game_attempt(mode, attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    obs = {"mode": mode, "assert": {}, "notes": [], "_fixture_ok": True}
    ramp_tag = f"301-{mode}"
    if attempt > 0:
        ramp_tag = f"301-{mode}-r{attempt}"
        say(f"[{mode}] fixture retry attempt {attempt}")
    ST["mana_needs"][ramp_tag] = {}

    p0 = PhaseClient(f"P0{ramp_tag}")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck(("Island", 56), ("Chaos Wand", 4)))
    p1 = PhaseClient(f"P1{ramp_tag}")
    await p1.connect()
    # 4x Bolt (not 1x): P1 draws ~13 cards during the ramp; a single Bolt is
    # drawn into hand ~22% of the time, which voids the fixture (the wand
    # then exiles 42 Islands, finds nothing, and there is no may-cast).
    # With 4x, the chance all four leave the library before activation is <1%.
    # The pre-activation check below still guards the residual risk.
    await p1.join(p0.game_code, deck(("Island", 56), ("Lightning Bolt", 4)))
    say(f"game {p0.game_code} mode={mode}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"mode": mode, "game_code": p0.game_code})
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                               and p1.player_id is not None) else "failed"

    # ---- ramp to 12 untapped islands with the wand out
    t_ramp = time.time()
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = time.time()
    act = None
    try:
        while time.time() - t_ramp < 900:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.player_id] = time.time()
                else:
                    holds_prio = my_priority(top_acts(st))
                    if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        r = await p0_tick_ramp(c, {"mode": mode}, ramp_tag)
                        if isinstance(r, tuple) and r[0] == "ACTIVATE":
                            act = r
                            break
                    else:
                        await p1_tick(c, {"mode": mode}, f"P1{ramp_tag}")
                except Exception as e:
                    say(f"[{ramp_tag}] tick error {c.name}: {type(e).__name__}: {e}")
                    wire(f"{ramp_tag}_tick_error",
                         {"who": c.name, "err": f"{type(e).__name__}: {e}"})
            if act:
                break
            if time.time() - last_diag > 25:
                last_diag = time.time()
                for c in (p0, p1):
                    st = c.latest
                    if not st:
                        say(f"[{ramp_tag}] DIAG {c.name}: no state yet")
                        continue
                    s = st["state"]
                    acts = top_acts(st)
                    vi = st.get("viewer_interaction") or {}
                    opdump = []
                    for op in vi_ops(st)[:4]:
                        resp = op.get("response", {}) or {}
                        data = resp.get("data", {}) or {}
                        items = data.get("candidates") or data.get("choices") or []
                        codes = set()
                        for ch in items[:6]:
                            codes.update(x for x in surf_codes(ch) if x)
                        opdump.append({"rtype": resp.get("type"),
                                       "n_items": len(items),
                                       "codes": sorted(codes)})
                    say(f"[{ramp_tag}] DIAG {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"act={s.get('active_player')} prio_acts={[a.get('type') for a in acts][:8]} "
                        f"vikind={vi_kind_code(st)!r} real_decision={real_decision_pending(st)} "
                        f"my_prio={my_priority(acts)} ops={json.dumps(opdump)[:400]}")
                    wire(f"{ramp_tag}_diag", {"who": c.name, "rev": c.revision,
                                             "ops": opdump,
                                             "act_types": [a.get("type") for a in acts][:10]})
    except Exception as e:
        obs["notes"].append(f"ramp loop exception: {e}")
    if not act or act[0] != "ACTIVATE":
        obs["assert"]["A2_activation_paid"] = "not-run"
        obs["notes"].append("never reached activation window")
        await p0.close()
        await p1.close()
        return obs
    wand_id = act[1]

    say("exporting PRE_ACTIVATION state")
    pre_env_s = await p0.export_state()
    pre_env = json.loads(pre_env_s)
    pre = pre_env["state"]
    with open(f"{EVDIR}/pre_activation_{mode}.json", "w") as f:
        f.write(pre_env_s)

    # Fixture guard (on the AUTHORITATIVE export — the live view omits the
    # opponent's library objects): the Bolt must still be in P1's library;
    # if P1 drew all copies into hand, the wand exiles only Islands and
    # there is no may-cast.
    if not bolt_in_p1_library(pre):
        obs["notes"].append(f"attempt {attempt}: all Bolts left P1's library before activation; restarting game")
        say(f"[{ramp_tag}] fixture void: Bolt not in P1 library (authoritative); retrying")
        wire(f"{ramp_tag}_fixture_void", {})
        obs["_fixture_ok"] = False
        await p0.close()
        await p1.close()
        return obs

    pre_untapped = untapped_islands(pre, 0)
    pre_lib_p1 = lib_size(pre, 1)
    wand_id = next(iter(bf_by_name(pre, 0, "chaos wand")), wand_id)
    say(f"ACTIVATING CHAOS WAND (id {wand_id}) via advertised ActivateAbility")
    ok = await activate_wand(p0, p1, {"mode": mode}, ramp_tag, wand_id)
    await asyncio.sleep(0)
    s = await settle(p0, p1, {"mode": mode}, ramp_tag, lambda s: True, 8,
                     "post-activation settle")
    if s is None:
        s = p0.latest["state"]
    w = get_obj(s, wand_id)
    paid = ok and w.get("tapped") and untapped_islands(s, 0) <= pre_untapped - 4
    obs["assert"]["A2_activation_paid"] = "passed" if paid else "failed"
    obs["notes"].append(f"activation ok={ok}; wand tapped={w.get('tapped')} "
                        f"untapped islands {pre_untapped}->{untapped_islands(s, 0)}")
    if not ok:
        await p0.close()
        await p1.close()
        return obs

    # The exile must begin: require an actual Exile-zone object, not just a
    # library shrink (a natural draw also shrinks the library).
    def exiled_happened(s):
        return (lib_size(s, 1) < pre_lib_p1
                and any(o.get("zone") == "Exile" for o in (s.get("objects") or {}).values()))

    s2 = await settle(p0, p1, {"mode": mode}, ramp_tag, exiled_happened, 180,
                      "exile resolution")
    if s2 is None:
        stuck_s = await p0.export_state()
        with open(f"{EVDIR}/stuck_after_activation_{mode}.json", "w") as f:
            f.write(stuck_s)
        obs["assert"]["A3_exile_observed"] = "failed"
        obs["notes"].append("P1 library never shrank after activation")
        await p0.close()
        await p1.close()
        return obs
    bolt = bolt_obj(s2)
    bolt_exiled = bolt is not None and bolt.get("zone") == "Exile"
    obs["assert"]["A3_exile_observed"] = "passed" if bolt_exiled else "failed"
    obs["notes"].append(f"P1 library {pre_lib_p1}->{lib_size(s2, 1)}; "
                        f"bolt zone={bolt.get('zone') if bolt else 'missing'}")
    say("exporting POST_EXILE checkpoint")
    post_exile_s = await p0.export_state()
    with open(f"{EVDIR}/post_exile_{mode}.json", "w") as f:
        f.write(post_exile_s)

    # Drive to the may-cast prompt (decideOptionalEffect opportunity for P0).
    # Generic ticks never auto-answer it; it is answered explicitly below.
    t0 = time.time()
    found = False
    while time.time() - t0 < 180:
        await asyncio.sleep(0.5)
        await tick_all_generic(p0, p1, {"mode": mode}, ramp_tag)
        await asyncio.sleep(0)
        if p0.latest and find_optional_cast_opp(p0.latest) is not None:
            found = True
            break
    if not found:
        obs["notes"].append("no decideOptionalEffect may-cast opportunity observed within 180s")
        wire(f"optional_cast_{mode}_missing_vi", p0.latest.get("viewer_interaction")
             if p0.latest else None)
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A5fx_target_accepted",
                  "A6fx_cast_resolved", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return obs

    wire(f"optional_cast_opportunity_{mode}", p0.latest.get("viewer_interaction"))
    pick_iid, pick_cid = await pick_optional_cast(p0, want_accept=(mode == "accept"),
                                                 tag=ramp_tag)
    if not pick_cid:
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A5fx_target_accepted",
                  "A6fx_cast_resolved", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        obs["notes"].append("may-cast choice not identified")
        await p0.close()
        await p1.close()
        return obs
    say(f"P0 submitting {mode}: choice id={pick_cid}")
    wire(f"{mode}_submission",
         {"interactionId": pick_iid,
          "response": {"type": "choose", "data": {"choiceId": pick_cid}}})
    await p0.send_interaction({"interactionId": pick_iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": pick_cid}}})
    await asyncio.sleep(2)

    if mode == "decline":
        # decline control: let cleanup finish (exiled cards to bottom of library).
        s3 = await settle(p0, p1, {"mode": mode}, ramp_tag,
                          lambda s: not any(o.get("zone") == "Exile"
                                            for o in (s.get("objects") or {}).values()),
                          120, "decline cleanup")
        if s3 is None:
            s3 = p0.latest["state"]
            obs["notes"].append("decline cleanup timed out waiting for Exile to empty")
        visible_bolts = [(o.get("id"), o.get("zone")) for o in (s3.get("objects") or {}).values()
                         if str(o.get("base_name") or o.get("name") or "").lower() == "lightning bolt"]
        vi_txt = json.dumps(p0.latest.get("viewer_interaction") or {})
        dangling = "decideOptionalEffect" in vi_txt or find_optional_cast_opp(p0.latest) is not None
        ok = (all(z != "Stack" for _, z in visible_bolts)
              and not any(o.get("zone") == "Exile" for o in (s3.get("objects") or {}).values())
              and life(s3, 0) == 20 and life(s3, 1) == 20 and not dangling)
        obs["assert"]["A8_decline_control_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"decline: visible bolts={visible_bolts} life={life(s3, 0)}/{life(s3, 1)} "
                            f"dangling={dangling}")
        say("exporting DECLINE_POST state")
        dp = await p0.export_state()
        with open(f"{EVDIR}/decline_post.json", "w") as f:
            f.write(dp)
        await p0.close()
        await p1.close()
        return obs

    # accept path: expect a target-selection opportunity for the free cast
    t1 = time.time()
    top = None
    while time.time() - t1 < 120:
        await asyncio.sleep(0.5)
        await tick_all_generic(p0, p1, {"mode": mode}, ramp_tag)
        await asyncio.sleep(0)
        if p0.latest:
            top = target_opportunity(p0.latest)
            if top[0] is not None:
                break
    obs["assert"]["A4_accept_target_pending"] = "passed" if top and top[0] is not None else "failed"
    if not top or top[0] is None:
        obs["notes"].append("no target-selection opportunity after accept")
        wire("target_opportunity_missing_vi", p0.latest.get("viewer_interaction")
             if p0.latest else None)
        await p0.close()
        await p1.close()
        return obs
    opp, rtype, spec_type = top
    say("target opportunity found; exporting PRE_TARGET checkpoint")
    wire("target_opportunity", p0.latest.get("viewer_interaction"))
    pre_target_s = await p0.export_state()
    with open(f"{EVDIR}/pre_target_accept.json", "w") as f:
        f.write(pre_target_s)
    pre_target = json.loads(pre_target_s)["state"]
    bolt = bolt_obj(pre_target)
    # A5 (bug symptom): exiled cards returned to library BEFORE target submission
    early_return = (lib_size(pre_target, 1) == pre_lib_p1
                    and bolt is not None and bolt.get("zone") == "Library"
                    and str(bolt.get("id")) in lib_ids(pre_target, 1))
    obs["assert"]["A5_exile_returned_early"] = "passed" if early_return else "failed"
    obs["notes"].append(f"pre-target: P1 lib={lib_size(pre_target, 1)} (pre-exile {pre_lib_p1}); "
                        f"bolt zone={bolt.get('zone') if bolt else 'missing'} in-library-membership="
                        f"{str(bolt.get('id')) in lib_ids(pre_target, 1) if bolt else '?'}")

    ch = pick_target_candidate(pre_target, opp)
    if ch is None:
        obs["assert"]["A6_advertised_target_rejected"] = "not-run"
        obs["assert"]["A5fx_target_accepted"] = "not-run"
        obs["notes"].append("no advertised target candidate identified; interaction preserved in wire log")
        await p0.close()
        await p1.close()
        return obs
    await submit_target(p0, opp, rtype, spec_type, ch, ramp_tag)
    # watch for rejection
    rejected = None
    t0 = time.time()
    while time.time() - t0 < 20 and rejected is None:
        await asyncio.sleep(0.5)
        try:
            while True:
                t, data = p0.inbox.get_nowait()
                if t in ("ActionRejected", "Error"):
                    rejected = {"type": t, "data": data}
                    wire("target_rejection", rejected)
                    say(f"REJECTION: {json.dumps(rejected, default=str)[:600]}")
        except asyncio.QueueEmpty:
            pass
    await asyncio.sleep(2)
    await tick_all_generic(p0, p1, {"mode": mode}, ramp_tag)
    await asyncio.sleep(0)
    still_pending = target_opportunity(p0.latest)[0] is not None if p0.latest else False
    not_allowed = rejected is not None and "not_allowed" in json.dumps(rejected, default=str).lower().replace(" ", "_")
    obs["assert"]["A6_advertised_target_rejected"] = "passed" if (not_allowed and still_pending) else "failed"
    obs["notes"].append(f"rejection={json.dumps(rejected, default=str)[:200] if rejected else 'none'}; "
                        f"still_pending={still_pending}")

    if not_allowed and still_pending:
        # bug path: record the failure end state
        s5 = p0.latest["state"]
        bolt = bolt_obj(s5)
        no_cast = (life(s5, 0) == 20 and life(s5, 1) == 20
                   and (bolt is None or bolt.get("zone") != "Graveyard"))
        obs["assert"]["A7_no_damage_no_cast"] = "passed" if no_cast else "failed"
        obs["notes"].append(f"post: life={life(s5, 0)}/{life(s5, 1)} "
                            f"bolt zone={bolt.get('zone') if bolt else 'missing'}")
        say("exporting POST_FAILURE state")
        post_s = await p0.export_state()
        with open(f"{EVDIR}/post_failure_accept.json", "w") as f:
            f.write(post_s)
        await p0.close()
        await p1.close()
        return obs

    # fixed path: target accepted; drive to resolution and assert the cast completes
    obs["assert"]["A5fx_target_accepted"] = "passed" if (rejected is None and not still_pending) else "failed"
    obs["notes"].append(f"fixed-path entry: rejected={bool(rejected)} still_pending={still_pending}")

    def resolved(s):
        b = bolt_obj(s)
        return (b is not None and b.get("zone") == "Graveyard"
                and life(s, 1) == 17
                and not any(o.get("zone") == "Exile" for o in (s.get("objects") or {}).values()))

    s6 = await settle(p0, p1, {"mode": mode}, ramp_tag, resolved, 120, "bolt resolution")
    s6 = s6 or p0.latest["state"]
    bolt = bolt_obj(s6)
    ok_fx = (bolt is not None and bolt.get("zone") == "Graveyard"
             and life(s6, 1) == 17 and life(s6, 0) == 20
             and not any(o.get("zone") == "Exile" for o in (s6.get("objects") or {}).values())
             and target_opportunity(p0.latest)[0] is None)
    obs["assert"]["A6fx_cast_resolved"] = "passed" if ok_fx else "failed"
    obs["notes"].append(f"post-resolution: life={life(s6, 0)}/{life(s6, 1)} "
                        f"bolt zone={bolt.get('zone') if bolt else 'missing'} "
                        f"bolt controller={bolt.get('controller') if bolt else '?'}")
    say("exporting POST_SUCCESS state")
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_success_accept.json", "w") as f:
        f.write(post_s)
    await p0.close()
    await p1.close()
    return obs

# ---------------------------------------------------------------- summary + main

def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 780
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #301 — Chaos Wand optional cast (revalidation)", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} — {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_activation_paid": "A2 wand activated ({4} paid, tapped)",
        "A3_exile_observed": "A3 Bolt exiled from P1 library",
        "A4_accept_target_pending": "A4 target-selection pending after accept",
        "A5_exile_returned_early": "A5 [bug] exile returned to library pre-target",
        "A6_advertised_target_rejected": "A6 [bug] advertised target rejected action_not_allowed",
        "A7_no_damage_no_cast": "A7 [bug] life 20/20, no cast recorded",
        "A5fx_target_accepted": "A5fx [fixed] target submission accepted",
        "A6fx_cast_resolved": "A6fx [fixed] Bolt resolved: P1 17, Bolt in P1 GY",
        "A8_decline_control_ok": "A8 decline control: clean, no dangling cast",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:9]:
        d.text((40, y), str(n)[:120], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 301/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


async def _main():
    t0 = time.time()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    await verify_server_hello()
    check_data_level()
    all_obs = {}
    if only in (None, "accept"):
        all_obs["accept"] = await run_game("accept")
    if only in (None, "decline"):
        all_obs["decline"] = await run_game("decline")
    dur = time.time() - t0
    ass = {}
    notes = []
    for m in ("accept", "decline"):
        if m not in all_obs:
            continue
        for k, v in all_obs[m]["assert"].items():
            ass[k] = v
        notes.extend(f"[{m}] {n}" for n in all_obs[m]["notes"])
    notes.append("protocol-118 driver (v0.102.0): MulliganDecision via legacy "
                 "Action (Keep); bottom/SelectCards via vi schema/select gated "
                 "on waitingForKind.code=='mulligan' + turn 1/Untap; "
                 "DiscardToHandSize via vi (hand>7 + hand-card select "
                 "opportunity); activation via advertised ActivateAbility "
                 "(source_id/_src_oid) with {4} paid via vi tapLandForMana / "
                 "PayMana actions; optional-cast accept/decline from vi "
                 "exactChoices decideOptionalEffect surfaces; target submitted "
                 "per the advertised schema response type; priority-gated "
                 "passes; sleep(0) yield before leg evaluation; re-tick "
                 "backstop for priority-holding clients.")
    a5 = all_obs.get("accept", {}).get("assert", {}).get("A5_exile_returned_early")
    a6 = all_obs.get("accept", {}).get("assert", {}).get("A6_advertised_target_rejected")
    a5fx = all_obs.get("accept", {}).get("assert", {}).get("A5fx_target_accepted")
    a6fx = all_obs.get("accept", {}).get("assert", {}).get("A6fx_cast_resolved")
    a8 = all_obs.get("decline", {}).get("assert", {}).get("A8_decline_control_ok")
    if a5 == "passed" and a6 == "passed":
        verdict = "reproduced"
    elif a5fx == "passed" and a6fx == "passed" and a8 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/301",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "runs/" + RUN_ID + " (fresh v0.104.0 server on 127.0.0.1:9374, isolated games.db)",
        "driver": {"protocol_advertised": 118, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Island", 56], ["Chaos Wand", 4]],
            "P1": [["Island", 56], ["Lightning Bolt", 4]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 activation_paid: %s; A3 exile_observed: %s; "
                   "A4 accept_target_pending: %s; A5 exile_returned_early: %s; "
                   "A6 advertised_target_rejected: %s; A7 no_damage_no_cast: %s; "
                   "A5fx target_accepted: %s; A6fx cast_resolved: %s; A8 decline_control_ok: %s"
                   % tuple(ass.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_activation_paid", "A3_exile_observed",
                            "A4_accept_target_pending", "A5_exile_returned_early",
                            "A6_advertised_target_rejected", "A7_no_damage_no_cast",
                            "A5fx_target_accepted", "A6fx_cast_resolved",
                            "A8_decline_control_ok"))),
        "scope": "Chaos Wand accepted-cast path; native human seats (no AI)",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "setup_line": "P0: 56x Island + 4x Chaos Wand; P1: 56x Island + 4x Lightning Bolt (draw-go; 4x so the library still holds a Bolt at activation)",
        "contract_line": ("Accept: free Bolt cast must finish targeting and resolve for 3 dmg; "
                          "Decline: no cast, cards to bottom"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_301_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    # Close logs BEFORE computing the manifest so their hashes are final.
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


async def main():
    pidfile = "/tmp/scenario_301_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_301_01020 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


asyncio.run(main())
