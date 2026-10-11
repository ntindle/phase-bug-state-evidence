#!/usr/bin/env python3
"""Issue #6250 re-validation on v0.106.0 (protocol 126): mechanical port of the
verified v0.104.0 scenario_6250_01040.py (protocol 118; verdict not-reproduced
2026-10-08). Only the release pin, build commit, ServerHello assertions, and
run/provenance strings changed for this freshness re-validation; interaction
conventions are unchanged on protocol 126 (verified by the v0.106.0 #301 run
today). DRIVER CHANGE (e) [2026-10-09]: decline-leg A4_attach_spec is
inapplicable by design (trigger declined, no selection prompt can appear), so
the scenario now records it as "not-run" and the verdict rule accepts
passed-or-not-run on the decline leg; the in-code verdict rule previously
required every decline assertion to be passed. DRIVER CHANGE (f) [2026-10-09]:
attach prompts are answered per-firing (the `not G["attach_answered"]` gate
is removed; SUBMITTED_OPPS is already keyed by interactionId) -- the gate
stalled the accept leg on the second firing's fresh prompt. DRIVER CHANGE (g)
[2026-10-09]: mulligan aggressively for Ardenn (cap 3); the settle gate now
reads the real stack list and both "BeginCombat"/"BeginningOfCombat" phase
spellings (protocol 126 changed both wire shapes).

DRIVER-FIXED RUN (c) [2026-10-06, original text preserved]: mechanical port of
scenario_6250_01030d.py (protocol 118; verified 2026-10-06, verdict
not-reproduced). A4 reads the constraint min/max at
spec.data.constraint.data; decline leg gates settle on pre_exported
so the decline control runs under full setup. Corrected verdict: NOT-REPRODUCED. (d): may_count tracks genuine
ACCEPTs only; deferral-declines counted separately so the
no-prompt backstop cannot misfire on setup-incomplete declines.)
fixes classify_schema_opp to read card_types (not type_line/type) so the
engine's attachment-selection prompt is recognized; answers it with ALL
advertised candidates; defers ACCEPT until all three attachments are on
the battlefield (declines earlier triggers as setup-incomplete).
Ardenn, Intrepid Archaeologist -- beginning-of-combat trigger attaches only
ONE Aura/Equipment instead of any number.

Reported outcome: "At the beginning of combat on your turn, you may attach
any number of Auras and Equipment you control to target permanent or player"
resolves a single attachment onto a single target.

Game A (accept): P0 controls Ardenn + Holy Strength (aura, attached on cast)
+ Plate Armor + Colossus Hammer (equipment, unattached), moves to combat,
accepts the optional trigger, targets Ardenn, selects attachments. Expect ALL
THREE attachments on Ardenn after resolution; the bug moves only one.
Game B (decline control): same setup, declines the optional trigger. Expect
NO attachment moves.

Behavioral contract / verdict rule:
  reproduced     iff the trigger resolves moving fewer attachments than the
                 controller selected (the engine caps the selection at 1).
  not-reproduced iff all three attachments end up on Ardenn (A) and the
                 decline control moves nothing (B).
  blocked        iff the game cannot be driven to the Ardenn trigger.

Protocol-126 driver conventions (from verified scenario_301_01050.py):
  - waiting_for is gone; priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision via legacy Action gated on the legal action; bottom via
    vi schema/select gated on waitingForKind.code == 'mulligan' AND turn 1 /
    Untap; DiscardToHandSize via vi gated on hand>7 + schema/select offering
    hand cards (generic 'choose' kind code).
  - CastSpell via legacy Action; mana via legacy PayMana actions and vi
    tapLandForMana menus driven by ST['mana_needs'].
  - Optional "you may" via vi exactChoices decideOptionalEffect
    (role/value accept markers); target + attachment selection via vi schema
    opportunities (select/sequence), classified by candidate references.
  - await asyncio.sleep(0) yield after the priority gate before leg
    evaluation; export-only checkpoints fall through to the priority pass;
    never return after an export while holding priority.
  - deck schema {"name", "main_deck": [...]}; client.py HELLO advertises 126
    (exact match enforced).
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6250_126")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-6250-reval-v01060-20261010-1911")
ISSUE = 6250
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.106.0",
    "build_commit": "29e0db3",
    "protocol_version": 126,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.106.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-10: latest stable release v0.106.0 (published "
               "2026-10-10T17:09:24Z) == pinned release dir (minisign-verified "
               "by the 17:45 run); ServerHello "
               "0.106.0/29e0db3/protocol 126 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server process on 127.0.0.1:9374 (pid 234971) reused from "
               "owning run run-5474-reval-v01060-20261010-1745 "
               "(backfill-owned, not restarted)"),
}
for _f, _k in (("server/releases/v0.106.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.106.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.106.0/data/draft-pools.json", "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = hashlib.sha256(
        open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"))

ARDENN = "ardenn, intrepid archaeologist"
HOLY = "holy strength"
PLATE = "plate armor"
HAMMER = "colossus hammer"
ATTACHMENTS = [HOLY, PLATE, HAMMER]

A_P0_DECK = deck(("Ardenn, Intrepid Archaeologist", 8), ("Holy Strength", 8),
                 ("Plate Armor", 8), ("Colossus Hammer", 8), ("Plains", 28))
A_P1_DECK = deck(("Mountain", 60))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ---------------------------------------------------------------- state helpers

def oname(o):
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return oname(get_obj(state, oid))


def hand_ids(state, pid):
    return [str(x) for x in (state.get("players") or [])[pid].get("hand", [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_find(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def untapped_plains(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) == "plains"]


def is_land(o):
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t or oname(o) in ("plains", "mountain", "island",
                                       "swamp", "forest")


def attached_to_oid(o):
    """Resolve which host oid an attachment is attached to, or None."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("attached_to", "attached_to_id", "attachedTo",
                         "attachedToId", "host_id", "host_oid"):
                    if isinstance(v, int):
                        found.append(str(v))
                    elif isinstance(v, dict) and isinstance(v.get("data"), int):
                        found.append(str(v["data"]))
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(o)
    return found[0] if found else None


def attachments_on(state, host_oid):
    """Names of attachments (aura/equipment) attached to host_oid."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        t = type_text(o)
        if "aura" not in t and "equipment" not in t:
            continue
        if attached_to_oid(o) == str(host_oid):
            out.append(oname(o))
    return out


def stack_triggered_abilities(state):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Stack" and \
                "triggered" in str(o.get("type") or o.get("kind") or "").lower():
            out.append((oid, o))
    return out


# ---------------------------------------------------------------- 106 primitives

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
    return real_decision_pending(st)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": a["data"]}
    msg = {k: v for k, v in msg.items() if not k.startswith("_")}
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
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
    assert str(ver).startswith("0.106.0"), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"
    assert str(build) == "29e0db3", f"unexpected build {build}"


def check_data_level():
    notes = []
    ok = True
    ardenn = CARD_DATA.get("ardenn, intrepid archaeologist") or {}
    oracle = str(ardenn.get("oracle_text") or "")
    if "attach any number" not in oracle.lower():
        ok = False
        notes.append("ardenn oracle text mismatch in pinned card-data.json")
    else:
        notes.append("ardenn oracle verified: 'attach any number of Auras and Equipment'")
    cost = ardenn.get("mana_cost") or {}
    notes.append(f"ardenn pinned mana_cost datum: {cost} "
                 "(real-world card is {2}{W}{R}; engine datum governs this run)")
    for nm, _ in (("holy strength", None), ("plate armor", None), ("colossus hammer", None)):
        if nm not in CARD_DATA:
            ok = False
            notes.append(f"missing card-data entry: {nm}")
    say("data check: " + "; ".join(notes))
    wire("data_check", {"ok": ok, "notes": notes})
    assert ok, "; ".join(notes)

# ---------------------------------------------------------------- game mechanics

SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"mana_needs": {}}


async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 126: MulliganDecision arrives as a legacy legal action.
    Mulligan aggressively for Ardenn (the fixture's key card; 8x density
    still whiffs ~3% of 20-card samples). Keep after 3 mulligans
    regardless. P1 is a dummy and always keeps. (2026-10-09)"""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mulligan", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    state = st["state"]
    names = hand_names(state, pid)
    n_mull = MULLIGAN_COUNT.get(tag, 0)
    want_ardenn = tag.startswith("P0-") and ARDENN not in names
    if want_ardenn and n_mull < 3:
        MULLIGAN_COUNT[tag] = n_mull + 1
        say(f"[{tag}] mulligan #{n_mull + 1} "
            f"(no Ardenn in {len(names)}-card hand)")
        wire("mulligan_take", {"who": tag, "n": n_mull + 1})
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
    else:
        reason = ("Ardenn found" if ARDENN in names
                  else "mulligan cap reached" if tag.startswith("P0-")
                  else "dummy seat")
        say(f"[{tag}] keeping {len(names)}-card hand ({reason})")
        wire("mulligan_keep", {"who": tag, "hand_size": len(names)})
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
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
            if any(str(_cand_reference(ch)) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm in ATTACHMENTS or nm == ARDENN:
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
        say(f"[{tag}] discarding to hand size via vi ({stype})")
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
    pool = sum(1 for o in untapped_plains(state, pid))
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


# ---------------------------------------------------------------- Ardenn prompt classifiers

def find_may_opp(st):
    """Any vi opportunity carrying a decideOptionalEffect code, regardless
    of response type (exactChoices on 103; possibly schema on 106)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        for ch in items:
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def schema_opps(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        if (spec.get("type") or "") in ("select", "sequence") \
                and data.get("candidates"):
            out.append((opp, spec, data.get("candidates")))
    return out


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 126: bottom-after-mulligan as per-card SelectCards legal
    actions plus a vi schema/select opportunity (kind stays 'mulligan').
    Gated on kind == 'mulligan' AND turn 1 / Untap. We always keep 7, so
    this is a safety net only."""
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

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in ATTACHMENTS or nm == ARDENN:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}},
                      tag)
    return True


def new_game_state(mode):
    return {"mode": mode, "done": False,
            "pre_exported": False, "pre_state": None, "pre_phase": None,
            "trigger_seen": False, "may_answered": False, "may_choice": None,
            "may_count": 0,
            "target_answered": False, "attach_answered": False,
            "attach_spec": {}, "attach_submitted": 0,
            "setup_deferred_declines": 0,
            "settle_ticks": 0, "post_exported": False, "post_state": None}


def type_text(o):
    """Protocol-106 authoritative exports carry card types in
    card_types {core_types, subtypes, supertypes}; type_line/type are
    absent. Fall back to them when present (older shapes)."""
    ct = o.get("card_types") or o.get("base_card_types") or {}
    parts = list(ct.get("core_types") or []) + list(ct.get("subtypes") or []) \
        + list(ct.get("supertypes") or [])
    tl = o.get("type_line") or o.get("type") or ""
    return (str(tl) + " " + " ".join(str(x) for x in parts)).lower()


def classify_schema_opp(state, candidates):
    """'attach' only if EVERY referenced candidate is an Aura/Equipment;
    'target' for anything broader (the trigger's 'target permanent or
    player' prompt lists all permanents incl. lands AND players)."""
    kinds = set()
    for ch in candidates:
        ref = _cand_reference(ch)
        if ref is None:
            kinds.add("player-or-unknown")
            continue
        try:
            o = get_obj(state, int(ref))
        except (TypeError, ValueError):
            kinds.add("unknown")
            continue
        t = type_text(o)
        if "aura" in t or "equipment" in t:
            kinds.add("attachable")
        else:
            kinds.add("other-permanent")
    if not kinds:
        return "unknown"
    if kinds == {"attachable"}:
        return "attach"
    return "target"


def pick_arden_target(state, candidates):
    for ch in candidates:
        ref = _cand_reference(ch)
        if ref is None:
            continue
        try:
            if obj_lname(state, int(ref)) == ARDENN:
                return ch
        except (TypeError, ValueError):
            continue
    return candidates[0] if candidates else None


async def answer_may(c, st, G, tag):
    opp = find_may_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    key = (tag, "may", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    # full opportunity dump for diagnosis (first sighting only)
    if f"{tag}_may_full" not in SUBMITTED_OPPS:
        SUBMITTED_OPPS.add(f"{tag}_may_full")
        wire(f"{tag}_may_opportunity_full", opp)
    state = st["state"]
    setup_ready = all(bf_find(state, 0, a) is not None for a in ATTACHMENTS)
    want_accept = (G["mode"] == "accept" and setup_ready)
    if G["mode"] == "accept" and not setup_ready:
        G["setup_deferred_declines"] = G.get("setup_deferred_declines", 0) + 1
        say(f"[{tag}] setup incomplete -- declining trigger "
            f"(deferral #{G['setup_deferred_declines']})")
        wire(f"{tag}_may_deferred_decline",
             {"reason": "setup incomplete"})
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    items = (resp.get("data", {}) or {}).get("choices") \
        or (resp.get("data", {}) or {}).get("candidates") or []
    chosen = None
    for ch in items:
        is_accept = None
        for sf in ch.get("surfaces", []) or []:
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "attach" in txt or "yes" in txt \
                    or ("may" in txt and "not" not in txt):
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt \
                    or txt.strip().startswith("no"):
                is_accept = False
        if is_accept == want_accept:
            chosen = ch
            break
    if chosen is None:
        wire(f"{tag}_may_no_matching_choice",
             {"want_accept": want_accept,
              "choices": [choice_text(ch) for ch in items]})
        say(f"[{tag}] WARNING: no may-choice matching want_accept={want_accept}")
        return False
    SUBMITTED_OPPS.add(key)
    G["may_answered"] = True
    if want_accept:
        G["may_count"] = G.get("may_count", 0) + 1
    else:
        G["decline_count"] = G.get("decline_count", 0) + 1
    G["may_choice"] = choice_text(chosen)[:120]
    say(f"[{tag}] answering Ardenn 'you may': "
        f"{'ACCEPT' if want_accept else 'DECLINE'} "
        f"({choice_text(chosen)[:60]})")
    wire(f"{tag}_may_answer", {"want_accept": want_accept,
                               "choice": choice_text(chosen)[:120],
                               "iid": iid, "rtype": rtype})
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype,
                                           "data": {"choiceIds": [chosen["id"]]}}},
                          tag)
    else:
        await answer_vi(c, opp, chosen, tag)
    return True


async def answer_schema_target(c, st, G, tag, opp, candidates):
    iid = opp.get("interactionId")
    key = (tag, "target", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    ch = pick_arden_target(st["state"], candidates)
    if ch is None:
        return False
    SUBMITTED_OPPS.add(key)
    G["target_answered"] = True
    say(f"[{tag}] answering target selection: Ardenn")
    wire(f"{tag}_target_answer", {"iid": iid, "choice": choice_text(ch)[:120]})
    resp = opp.get("response", {}) or {}
    spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
    stype = spec.get("type") or "select"
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": stype,
                                       "data": {"choiceIds": [ch["id"]]}}}, tag)
    return True


async def answer_attach_choice(c, st, G, tag, opp, spec, candidates):
    iid = opp.get("interactionId")
    key = (tag, "attach", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    stype = spec.get("type") or "sequence"
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
             ).get("data", {}) or {}
    G["attach_spec"] = {
        "type": stype,
        "min": con.get("min", spec.get("min")),
        "max": con.get("max", spec.get("max")),
        "ncandidates": len(candidates),
        "candidate_names": [choice_text(ch)[:60] for ch in candidates],
        "raw_constraint": con,
    }
    wire(f"{tag}_attach_spec", G["attach_spec"])
    say(f"[{tag}] attachment prompt spec: type={stype} "
        f"min={spec.get('min')} max={spec.get('max')} "
        f"candidates={len(candidates)}")
    # Attempt maximal attachment: select ALL advertised candidates.
    ids = [ch["id"] for ch in candidates]
    SUBMITTED_OPPS.add(key)
    G["attach_answered"] = True
    G["attach_submitted"] = len(ids)
    say(f"[{tag}] submitting attachment choice: {len(ids)} candidate(s)")
    wire(f"{tag}_attach_answer", {"iid": iid, "n": len(ids), "ids": ids})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": stype,
                                       "data": {"choiceIds": ids}}}, tag)
    return True


async def p1_tick(c, G, tag):
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
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, G, tag):
    st = c.latest
    if not st:
        return None
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return None
    if await do_bottom(c, acts, st, 0, tag):
        return None
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return None
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return None
    if "DeclareBlockers" in atypes:
        return None
    if await pay_tick(c, acts):
        return None
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return None

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # ---- explicit Ardenn decisions first
    if find_may_opp(st) is not None:
        if not G["trigger_seen"]:
            G["trigger_seen"] = True
            wire(f"{tag}_trigger_seen", {"via": "may_prompt"})
            say(f"[{tag}] Ardenn trigger observed (may prompt)")
        await answer_may(c, st, G, tag)
        return None
    for trig_oid, trig in stack_triggered_abilities(state):
        src = trig.get("source") or {}
        if ARDENN in str(src.get("name") or src).lower() or \
                ARDENN in str(trig.get("source_name") or "").lower():
            if not G["trigger_seen"]:
                G["trigger_seen"] = True
                wire(f"{tag}_trigger_seen",
                     {"via": "stack", "oid": trig_oid,
                      "trigger": {k: trig.get(k) for k in
                                  ("name", "type", "kind", "controller")}})
                say(f"[{tag}] Ardenn trigger on stack (oid {trig_oid})")
    if not G["done"]:
        for opp, spec, candidates in schema_opps(st):
            kind = classify_schema_opp(state, candidates)
            iid = opp.get("interactionId")
            # full dump of trigger-window schema opportunities (once each)
            if f"{tag}_full_{iid}" not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(f"{tag}_full_{iid}")
                wire(f"{tag}_schema_full", {"iid": iid, "kind": kind,
                                            "opportunity": opp})
            # Per-firing answers: Ardenn's trigger can fire more than once per
            # game, and the engine issues a FRESH attach prompt (new iid) for
            # each firing. Gate on the per-iid SUBMITTED_OPPS key inside
            # answer_attach_choice, never on the per-leg attach_answered
            # boolean -- gating per-leg ignores the second firing's prompt
            # and stalls the leg on its deadline. (2026-10-09; the 2026-10-08
            # lesson documented this failure mode on scenario_6250_01040.py
            # but the gate was never actually removed.)
            if kind == "attach":
                await answer_attach_choice(c, st, G, tag, opp, spec,
                                           candidates)
                return None
            if kind == "target":
                # Holy Strength cast targeting OR Ardenn trigger targeting:
                # both want Ardenn.
                if (tag, "target", str(iid)) in SUBMITTED_OPPS:
                    continue
                await answer_schema_target(c, st, G, tag, opp, candidates)
                return None

    # ---- ramp: lands then the Ardenn package in cast order
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return None
        ardenn_oid = bf_find(state, 0, ARDENN)
        holy_oid = bf_find(state, 0, HOLY)
        plate_oid = bf_find(state, 0, PLATE)
        hammer_oid = bf_find(state, 0, HAMMER)
        hn = hand_names(state, 0)
        order = []
        if ardenn_oid is None and ARDENN in hn:
            order.append((ARDENN, {"W": 1, "generic": 2}, 3))
        if ardenn_oid is not None and holy_oid is None and HOLY in hn:
            order.append((HOLY, {"W": 1, "generic": 0}, 1))
        if plate_oid is None and PLATE in hn:
            order.append((PLATE, {"W": 1, "generic": 2}, 3))
        if hammer_oid is None and HAMMER in hn:
            order.append((HAMMER, {"W": 0, "generic": 1}, 1))
        for key, needs, pool in order:
            if len(untapped_plains(state, 0)) < pool:
                continue
            a, oid = cast_action_for(acts, state, key)
            if a is not None:
                ST["mana_needs"][tag] = dict(needs)
                say(f"[{tag}] casting {key} (oid {oid})")
                wire("cast", {"who": tag, "card": key, "oid": oid})
                await submit_as_is(c, a)
                return None
        if not order and ardenn_oid is not None:
            # diagnostic: setup incomplete but nothing castable
            wire(f"{tag}_main_no_cast", {"hand": hand_names(state, 0),
                                         "untapped_plains": len(untapped_plains(state, 0)),
                                         "turn": state.get("turn_number")})
        # fully set up -> capture pre at PreCombatMain, then pass through
        if (ardenn_oid is not None and holy_oid is not None
                and plate_oid is not None and hammer_oid is not None
                and not G["pre_exported"]
                and state.get("phase") == "PreCombatMain"):
            pre = await export_state(c, f"{tag}_pre")
            G["pre_state"] = pre
            G["pre_phase"] = pre.get("phase")
            G["pre_exported"] = True
            say(f"[{tag}] pre.json captured "
                f"(turn {pre.get('turn_number')} {pre.get('phase')})")
            # export-only checkpoint: fall through to the priority pass

    # ---- settle detection: the trigger window is over once the may was
    # answered, the stack is clear of the trigger, no Ardenn decisions are
    # pending, and combat has moved past BeginningOfCombat (40 quiet ticks).
    # Accept mode: if the attach choice was answered, settle normally; if
    # the may was genuinely ACCEPTED 3+ times (not deferral-declines) and no
    # attachment-selection prompt ever appeared, that absence IS the
    # finding -- capture post and finish.
    settle_ready = G["pre_exported"] or (
        G["mode"] == "accept" and G.get("may_count", 0) >= 3
        and not G["attach_answered"])
    if G["may_answered"] and not G["post_exported"] and settle_ready:
        ardenn_pending = find_may_opp(st) is not None or any(
            classify_schema_opp(state, cands) == "attach"
            for _o, _s, cands in schema_opps(st))
        still_on_stack = bool(state.get("stack"))
        # v0.106.0/protocol-126: the engine serializes the phase as
        # "BeginCombat" (not "BeginningOfCombat") and stack ability objects
        # carry kind {"data": {"ability": ...}} with no "triggered" marker,
        # so gate on the actual stack list and both phase spellings.
        # (2026-10-09: the old gates failed open and the decline leg
        # exported post while the declined trigger was still resolving.)
        past_combat_start = state.get("phase") not in ("BeginningOfCombat",
                                                       "BeginCombat")
        if G["mode"] == "accept" and not G["attach_answered"] \
                and G.get("may_count", 0) >= 3 and past_combat_start \
                and not still_on_stack and not ardenn_pending:
            wire(f"{tag}_no_attach_prompt",
                 {"may_accepts": G["may_count"],
                  "note": "no attachment-selection prompt in 3+ ACCEPT cycles"})
            say(f"[{tag}] no attachment prompt in {G['may_count']} ACCEPT "
                f"cycles; capturing post.json")
            post = await export_state(c, f"{tag}_post")
            G["post_state"] = post
            G["post_exported"] = True
            G["done"] = True
            return "DONE"
        need_attach = (G["mode"] == "accept" and not G["attach_answered"])
        if (not ardenn_pending and not still_on_stack
                and past_combat_start and not need_attach):
            G["settle_ticks"] += 1
            if G["settle_ticks"] >= 40:
                post = await export_state(c, f"{tag}_post")
                G["post_state"] = post
                G["post_exported"] = True
                G["done"] = True
                say(f"[{tag}] post.json captured; game complete")
                return "DONE"
        else:
            G["settle_ticks"] = 0

    if state.get("turn_number", 0) and state.get("turn_number") > 25 \
            and not G["pre_exported"]:
        say(f"[{tag}] ABORT: setup not reached by turn 25")
        wire(f"{tag}_abort", {"reason": "setup timeout",
                              "hand": hand_names(state, 0)})
        try:
            abort_state = await export_state(c, f"{tag}_abort")
            with open(f"{EVDIR}/abort_{G['mode']}.json", "w") as f:
                json.dump({"state": abort_state}, f)
        except Exception as e:
            wire(f"{tag}_abort_export_failed", {"error": str(e)})
        return "ABORT"

    if decision_pending(st):
        return None
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
    return None


# ---------------------------------------------------------------- game runner

async def export_state(c, tag):
    raw = await c.export_state()
    env = json.loads(raw)
    # unwrap the export envelope: {"precast_shortcut_runtime":..., "state": <game>}
    st = env.get("state", env)
    wire(f"{tag}_export", {"keys": list(st.keys())[:8]})
    return st


async def run_game(mode):
    """mode in {'accept','decline'}. Returns dict of observations + assertions."""
    global PASSED_REV, SUBMITTED_OPPS, LAND_PLAYED_TURN, MULLIGAN_COUNT
    PASSED_REV = {}
    SUBMITTED_OPPS = set()
    LAND_PLAYED_TURN = {}
    MULLIGAN_COUNT = {}
    G = new_game_state(mode)
    tag = f"P0-{mode}"
    obs = {"mode": mode, "assertions": {}, "notes": []}
    A = obs["assertions"]

    p0 = PhaseClient(f"p0-{mode}")
    p1 = PhaseClient(f"p1-{mode}")
    await p0.connect()
    await p1.connect()
    att = await p0.create(A_P0_DECK, player_count=2)
    code = att.get("game_code")
    say(f"[{mode}] game created code={code}")
    wire(f"{mode}_game_created", {"game_code": code})
    await p1.join(code, A_P1_DECK)

    t0 = time.time()
    deadline = t0 + 600
    last_rev = (-1, -1)
    stall_since = time.time()
    try:
        while time.time() < deadline and not G["done"]:
            st0 = p0.latest
            st1 = p1.latest
            revs = (p0.revision, p1.revision)
            if revs != last_rev:
                last_rev = revs
                stall_since = time.time()
            elif time.time() - stall_since > 45:
                # re-tick backstop: nudge both clients if nothing moved
                stall_since = time.time()
                say(f"[{mode}] 45s without revision change; re-ticking")
            await p1_tick(p1, G, f"P1-{mode}")
            r = await p0_tick(p0, G, tag)
            if r == "DONE":
                break
            if r == "ABORT":
                obs["notes"].append("driver abort: unrecoverable state")
                break
            await asyncio.sleep(0.15)
    finally:
        await p0.close()
        await p1.close()

    # ---------------- assertions from saved states
    pre = G.get("pre_state") or {}
    post = G.get("post_state") or {}
    pre_objs = pre.get("objects", {})
    post_objs = post.get("objects", {})

    def find_in(objs, name, pid=0):
        for oid, o in objs.items():
            if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                    and oname(o) == name:
                return oid
        return None

    ardenn_pre = find_in(pre_objs, ARDENN)
    A["A1_setup_ok"] = ("passed" if (
        ardenn_pre is not None
        and all(find_in(pre_objs, a) is not None for a in ATTACHMENTS)
        and G.get("pre_phase") in ("PreCombatMain", "BeginningOfCombat"))
        else "failed")
    A["A2_trigger_fired"] = "passed" if G.get("trigger_seen") else "failed"
    A["A3_may_answered"] = "passed" if G.get("may_answered") else "failed"
    spec = G.get("attach_spec") or {}
    nc = spec.get("ncandidates", 0)
    maxv = spec.get("max")
    # The card says "any number": the advertised cap must cover every
    # available attachment. A finite max < ncandidates is the reported bug.
    A["A4_attach_spec"] = ("passed" if nc >= 1 and maxv is not None
                           and (maxv < 0 or maxv >= nc)
                           else "failed")
    if mode == "decline":
        # A4 is inapplicable on the decline leg: the optional trigger was
        # declined, so no attachment-selection prompt can appear. Record as
        # not-run instead of letting the nc>=1 formula fail it. (2026-10-09)
        A["A4_attach_spec"] = "not-run"
    obs["attach_cap_note"] = (f"advertised max={maxv} over {nc} candidates; "
                              "card text requires 'any number'")
    obs["attach_spec"] = spec

    if mode == "accept":
        ardenn_post = find_in(post_objs, ARDENN)
        attached = []
        if ardenn_post is not None:
            for oid, o in post_objs.items():
                if o.get("zone") != "Battlefield":
                    continue
                t = type_text(o)
                if "aura" not in t and "equipment" not in t:
                    continue
                if attached_to_oid(o) == str(ardenn_post):
                    attached.append(oname(o))
        obs["attached_post"] = sorted(attached)
        A["A5_outcome"] = ("passed" if all(a in attached for a in ATTACHMENTS)
                           else "failed")
    else:
        ardenn_post = find_in(post_objs, ARDENN)
        holy_oid = find_in(post_objs, HOLY)
        holy_host = attached_to_oid(post_objs.get(str(holy_oid), {})) \
            if holy_oid else None
        plate_oid = find_in(post_objs, PLATE)
        hammer_oid = find_in(post_objs, HAMMER)
        moved = ((holy_host != str(ardenn_post)) if (holy_oid and ardenn_post)
                 else True)
        if plate_oid and attached_to_oid(post_objs.get(str(plate_oid), {})):
            moved = True
        if hammer_oid and attached_to_oid(post_objs.get(str(hammer_oid), {})):
            moved = True
        obs["decline_hosts"] = {"holy": holy_host, "ardenn": ardenn_post}
        A["A5_outcome"] = "passed" if not moved else "failed"

    stack_empty = not (post.get("stack") or [])
    A["A6_cleanup"] = "passed" if (stack_empty and G.get("post_state")) \
        else "failed"

    # persist per-game evidence
    for key, fname in (("pre_state", f"pre_{mode}.json"),
                       ("post_state", f"post_{mode}.json")):
        st = G.get(key)
        if st is not None:
            with open(f"{EVDIR}/{fname}", "w") as f:
                json.dump({"state": st}, f)
    wire(f"{mode}_assertions", {"assertions": A, "notes": obs["notes"],
                                "attach_spec": spec})
    say(f"[{mode}] assertions: " +
        "; ".join(f"{k}={v}" for k, v in A.items()))
    return obs


# ---------------------------------------------------------------- finalize

def render_png():
    """Render summary.png from saved states + wire log (factual only)."""
    from PIL import Image, ImageDraw
    W, H = 1100, 780
    BG, PANEL, TEXT = (18, 20, 26), (26, 30, 38), (235, 238, 245)
    DIM, ACCENT = (150, 160, 175), (110, 180, 255)
    GREEN, RED, YELLOW = (110, 220, 140), (240, 120, 120), (240, 200, 110)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    def load_env(p):
        try:
            with open(os.path.join(EVDIR, p)) as f:
                return json.load(f)["state"]
        except (OSError, KeyError, ValueError):
            return None

    pre_a, post_a = load_env("pre_A.json") or load_env("pre_accept.json"), \
        load_env("post_A.json") or load_env("post_accept.json")
    pre_b, post_b = load_env("pre_B.json") or load_env("pre_decline.json"), \
        load_env("post_B.json") or load_env("post_decline.json")
    run = {}
    try:
        with open(os.path.join(EVDIR, "run.json")) as f:
            run = json.load(f)
    except OSError:
        pass

    def bf_list(st):
        out = []
        if not st:
            return out
        for oid, o in (st.get("objects") or {}).items():
            if o.get("zone") == "Battlefield" and o.get("controller") == 0:
                nm = oname(o)
                host = attached_to_oid(o)
                hn = ""
                if host:
                    ho = (st.get("objects") or {}).get(str(host), {})
                    hn = f" -> {oname(ho)}"
                out.append(f"{nm}{hn}")
        return sorted(out)

    y = 18
    d.text((24, y), "Issue #6250 — Ardenn, Intrepid Archaeologist", fill=TEXT)
    y += 24
    d.text((24, y), "beginning-of-combat trigger: 'attach ANY NUMBER of Auras "
           "and Equipment' (v0.106.0 / protocol 126)", fill=DIM)
    y += 30
    spec = ((run.get("games") or {}).get("accept") or {}).get("attach_spec", {})
    d.text((24, y), f"advertised attachment-selection spec: "
           f"type={spec.get('type')} min={spec.get('min')} "
           f"max={spec.get('max')} candidates={spec.get('ncandidates')}",
           fill=YELLOW if spec.get("max") == 1 else ACCENT)
    y += 30
    for label, pre, post in (("ACCEPT leg", pre_a, post_a),
                             ("DECLINE leg", pre_b, post_b)):
        d.text((24, y), label, fill=ACCENT)
        y += 22
        for tag2, st in (("pre ", pre), ("post", post)):
            names = bf_list(st)
            shown = ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
            d.text((40, y), f"{tag2}: {shown if shown else '(no state)'}",
                   fill=TEXT if st else DIM)
            y += 20
        y += 8
    y += 4
    d.text((24, y), "Assertions:", fill=TEXT)
    y += 22
    for gname in ("accept", "decline"):
        g = (run.get("games") or {}).get(gname) or {}
        for k, v in (g.get("assertions") or {}).items():
            col = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
            d.text((40, y), f"{gname}.{k}: {v}", fill=col)
            y += 20
        y += 4
    d.text((24, H - 30), "evidence: ntindle/phase-bug-state-evidence 6250/" +
           RUN_ID + " (authoritative exports; replay via scenario_6250_01060.py)",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    say(f"rendered {out} ({W}x{H})")
    wire("png_rendered", {"path": out})


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for f in files:
        h = hashlib.sha256(open(os.path.join(EVDIR, f), "rb").read()).hexdigest()
        lines.append(f"{h}  {f}")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as mf:
        mf.write("\n".join(lines) + "\n")
    # print (not say/wire): logs are already closed when this runs
    print(f"manifest.sha256 written for {len(lines)} files", flush=True)
    # self-verify
    bad = []
    for line in lines:
        h, _, name = line.partition("  ")
        h2 = hashlib.sha256(open(os.path.join(EVDIR, name.strip()), "rb").read()
                            ).hexdigest()
        if h2 != h:
            bad.append(name)
    assert not bad, f"manifest self-check failed: {bad}"
    print(f"manifest self-check passed for {len(lines)} files", flush=True)


async def main():
    await verify_server_hello()
    check_data_level()
    # parse evidence: oracle text from the pinned dataset
    ardenn = CARD_DATA.get("ardenn, intrepid archaeologist") or {}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"card": "Ardenn, Intrepid Archaeologist",
                   "oracle_text": ardenn.get("oracle_text"),
                   "mana_cost": ardenn.get("mana_cost"),
                   "source": "server/releases/v0.106.0/data/card-data.json"},
                  f, indent=1)

    games = {}
    games["accept"] = await run_game("accept")
    games["decline"] = await run_game("decline")

    # copy the driver into evidence (render_png runs later, after run.json)
    subprocess.run(["cp", __file__, f"{EVDIR}/scenario_6250_01060.py"],
                   check=True)

    # server log excerpts (trigger-relevant lines)
    excerpts = []
    try:
        with open(f"{BACKFILL}/runs/run-5474-reval-v01060-20261010-1745/server.log", errors="replace") as f:
            for line in f:
                ll = line.lower()
                if any(k in ll for k in ("trigger", "attach", "error", "warn",
                                        "ardenn", "modal", "effectzone")):
                    excerpts.append(line.rstrip()[:300])
    except OSError:
        pass
    with open(f"{EVDIR}/server_excerpts.log", "w") as f:
        f.write("\n".join(excerpts[-120:]))

    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "server_identity": SERVER_IDENTITY,
        "games": games,
        "verdict_rule": ("reproduced iff the trigger resolves moving fewer "
                         "attachments than the controller selected; "
                         "not-reproduced iff all three attach (accept) and "
                         "decline moves nothing (decline A4 not-run by "
                         "design); blocked otherwise"),
    }
    acc = games["accept"]["assertions"]
    dec = games["decline"]["assertions"]
    # decline-leg A4 is "not-run" by design (trigger declined); treat
    # not-run there as acceptable. Accept leg must have every assertion
    # passed. (2026-10-09)
    dec_ok = all(v in ("passed", "not-run") for v in dec.values()) and \
        dec.get("A4_attach_spec") == "not-run"
    if all(v == "passed" for v in acc.values()) and dec_ok:
        verdict = "not-reproduced"
    elif acc.get("A2_trigger_fired") == "passed" and (
            acc.get("A4_attach_spec") == "failed"
            or acc.get("A5_outcome") == "failed"):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    run["verdict"] = verdict
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    print(f"VERDICT: {verdict}")

    # render (say() needs RUNLOG open), then close logs BEFORE the manifest
    # so the hashes cover the final bytes of every file
    render_png()
    WIRE.close()
    RUNLOG.close()

    write_manifest()
    # validate: every JSON parses, PNG readable
    for f in os.listdir(EVDIR):
        if f.endswith(".json"):
            json.load(open(os.path.join(EVDIR, f)))
    im = __import__("PIL.Image", fromlist=["Image"]).open(
        os.path.join(EVDIR, "summary.png"))
    im.verify()
    print("evidence validated: JSON parses, manifest self-checks, PNG readable")


if __name__ == "__main__":
    asyncio.run(main())
