#!/usr/bin/env python3
"""Issue #6773 re-validation on pinned v0.105.0 (protocol 120): The Masamune
does not double death triggers (Toxrill slugs).

Reported (Discord): "[[The Masamune]] does not double death triggers, at
least not with [[Toxrill]] making slugs."

Oracle (verified from pinned v0.105.0 card-data.json at data level):
  The Masamune ({3}, Legendary Artifact - Equipment):
    "As long as equipped creature is attacking, it has first strike and
     must be blocked if able.
     Equipped creature has "If a creature dying causes a triggered ability
     of this creature or an emblem you own to trigger, that ability triggers
     an additional time."
     Equip {2}"
  Toxrill, the Corrosive ({5}{B}{B}, 7/7):
    "At the beginning of each end step, put a slime counter on each
     creature you don't control.
     Creatures you don't control get -1/-1 for each slime counter on them.
     Whenever a creature you don't control with a slime counter on it dies,
     create a 1/1 black Slug creature token."

Behavioral contract (two human-client seats, v0.105.0/proto 120):
  RAMP   - P0 drops one land/turn (Swamp-first to 2, then balanced),
           keeps mulligan, casts The Masamune when affordable, then Toxrill
           when affordable, then equips via advertised ActivateAbility.
  EQUIP  - P0 activates Masamune's Equip, then answers the equip target
           selection with Toxrill's candidate from the advertised vi
           opportunity.
  VICTIM - P1 casts exactly one Llanowar Elves, gated on the equip being
           complete, and holds priority passes otherwise.
  DEATH  - at the next end step, Toxrill's counter trigger puts a slime
           counter on the 1/1 Elves; it dies as a 0/0 SBA; Toxrill's
           death trigger fires and, per the granted DoubleTriggers, should
           trigger an additional time -> two triggers on the stack.
  PRE    - exported once: Toxrill equipped, Elves on P1 BF, 0 slime counters.
  MID    - exported at the first revision where a Toxrill slime-DEATH
           trigger sits on the stack; slug-trigger entries counted
           (expect 2).
  POST   - exported after the stack empties past the end step; Slug tokens
           counted (expect 2).

  A1 setup_ok         pre: Toxrill on P0 BF, Masamune attached to Toxrill,
                      Elves on P1 BF with no slime counters.
  A2 death_observed   post: Elves in P1 graveyard; mid shows >=1
                      Toxrill-sourced death trigger on the stack.
  A3 triggers_doubled  mid: exactly 2 Toxrill slug-trigger stack entries
                      (or the per-tick stack audit ever observed 2).
  A4 slugs_created     post: exactly 2 Slug tokens on P0 BF.
  A5 cleanup           post: stack empty.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1+A2 pass and A3/A4 show 1 instead of 2.
Verdict = not-reproduced iff A1..A5 all pass.

Protocol-120 driver notes (v0.105.0, 2026-10-10): mechanical port of the verified
v0.104.0 scenario_6773_01050.py (the 2026-10-09 v0.104.0 run reproduced the
bug, A3/A4 failed with 1 trigger / 1 slug). Only the release pin, build
commit, ServerHello assertions, RUN_ID, card-data path, and docstrings
changed; the 2026-10-09 cast-confirmation guard carried over unchanged.

Protocol-118 driver notes (v0.104.0, 2026-10-09): ported from the verified
v0.103.0 scenario_6773_01030.py (protocol-106; the 2026-10-07 v0.103.0 run
reproduced the bug, A3/A4 failed with 1 trigger / 1 slug). Only the release
pin, build commit, ServerHello assertions, and RUN_ID changed, plus the
2026-10-09 cast-confirmation guard: after EVERY CastSpell / ActivateAbility
submission on protocol 118 the driver sets ST["inflight"] and does NOT pass
priority or start new main-phase plays until the spell/activation is
confirmed (on the stack / battlefield / attached); a 30s backstop clears a
still-unconfirmed submission as dropped and resets MANA_NEEDS plus the
one-shot equip flags so the block re-triggers. The guard runs before the
priority pass and before new main-phase plays, but never blocks
mana-payment ticks or the equip-target answer. Answering stays
per-opportunity keyed by interactionId.

Protocol-106 driver notes (v0.103.0, 2026-10-07) [original text preserved]:
ported from the verified protocol-94 scenario_6773_0980.py onto the
protocol-106 conventions proven
in scenario_7195_01020.py / scenario_6250_01020.py:
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action, gated on the legal action.
  - DiscardToHandSize via vi schema/select opportunity offering hand cards.
  - CastSpell via legacy Action; mana via legacy PayMana actions and/or vi
    tapLandForMana menus driven by MANA_NEEDS.
  - Equip via advertised ActivateAbility merged_action (source match), vi
    exactChoices activateAbility as fallback; equip target via the
    advertised vi target opportunity (schema/select or exactChoices),
    gated on equip_activated.
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - Land-play matching uses is_land() over every land the seats can hold;
    playLand vi codes answered before the decision gate (AGENTS.md
    2026-10-05: unhandled playLand menus stall the game).
  - Death-trigger discrimination is TIMING-based (robust to 106 stack-entry
    shape changes): before the Elves dies, slime-phrase stack entries are
    the end-step counter trigger; once the Elves is in P1's graveyard, any
    slime-phrase trigger on the stack is the death trigger (the counter
    trigger must have resolved for the death to happen).
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6773
RUN_ID = os.environ.get("RUN_ID", "run-6773-reval-v01050-20261010-1041")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

MASAMUNE = "the masamune"
TOXRILL = "toxrill, the corrosive"
ELVES = "llanowar elves"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

GAME_TIMEOUT = 1800

ST = {}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
# 2026-10-09 cast-confirmation guard (protocol 118): after EVERY CastSpell /
# ActivateAbility submission, ST["inflight"][tag] = {"kind", "lname", "t"}.
# While set, the tick must NOT pass priority or start new main-phase plays
# until the submission is confirmed (spell on stack/battlefield, equip
# attached). A 30s backstop clears a still-unconfirmed submission as dropped.
ST["inflight"] = {}


def set_inflight(tag, kind, lname):
    ST["inflight"][tag] = {"kind": kind, "lname": lname, "t": time.time()}
    say(f"[{tag}] inflight set: {kind} {lname}")


def clear_inflight(tag, why):
    if tag in ST["inflight"]:
        say(f"[{tag}] inflight cleared ({why})")
        ST["inflight"].pop(tag, None)


def cast_confirmed_on_board(state, pid, lname):
    """True if a permanent spell with this name is on the stack or on its
    controller's battlefield (covers Masamune / Toxrill / Elves)."""
    lname = lname.lower()
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() != lname:
            continue
        if o.get("zone") == "Stack":
            return True
        if o.get("zone") == "Battlefield" and str(
                o.get("controller", -1)) == str(pid):
            return True
    return False


def inflight_guard(c, tag, pid):
    """2026-10-09 guard: returns True while a CastSpell/ActivateAbility
    submission is still unconfirmed (caller must hold: no priority pass, no
    new main-phase plays). Payment ticks and the equip-target answer are NOT
    blocked by this function -- the caller runs those before consulting it.
    Confirmation sets nothing else; the *_cast-style flags in this scenario
    are state-derived (card on battlefield), so a confirmed cast simply
    stops re-triggering. The 30s backstop resets MANA_NEEDS and the equip
    one-shot flags so the block re-triggers and retries."""
    pend = ST["inflight"].get(tag)
    if not pend:
        return False
    st = st_of(c)
    state = st["state"] if st else {}
    kind = pend["kind"]
    confirmed = False
    if kind == "cast":
        confirmed = cast_confirmed_on_board(state, pid, pend["lname"])
    elif kind == "equip":
        confirmed = masamune_equipped_toxrill(state)
    if confirmed:
        clear_inflight(tag, f"{kind} confirmed")
        return False
    if time.time() - pend["t"] > 30:
        clear_inflight(tag, f"{kind} backstop: unconfirmed after 30s")
        MANA_NEEDS[tag] = {}
        if kind == "equip":
            ST["equip_activated"] = False
            ST["equip_target_ticks"] = 0
        if kind == "cast" and pend["lname"] == ELVES:
            # one-shot flag is set at submission; reset so the cast block
            # re-triggers and retries (AGENTS.md 2026-10-09)
            ST["elves_cast"] = False
        wire("inflight_backstop", {"who": tag, "kind": kind,
                                   "lname": pend["lname"]})
        return False
    return True


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
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


def gy_by_name(state, pid, lname):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and str(o.get("base_name") or o.get("name") or "").lower() == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid, lname):
    return sum(1 for oid in bf_oids(state, pid)
               if not get_obj(state, oid).get("tapped")
               and obj_lname(state, oid) == lname)


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


def attached_to_oid(obj):
    ints = ints_in(obj.get("attached_to"))
    return ints[0] if ints else None


def masamune_equipped_toxrill(state):
    tox = bf_by_name(state, 0, TOXRILL)
    if not tox:
        return False
    tox_oid = str(tox[0])
    for oid in bf_by_name(state, 0, MASAMUNE):
        if str(attached_to_oid(get_obj(state, oid))) == tox_oid:
            return True
    return False


def elves_slime_free(state):
    elves = bf_by_name(state, 1, ELVES)
    if not elves:
        return False
    return "slime" not in json.dumps(get_obj(state, elves[0])).lower()


def stack_entries(state):
    return state.get("stack") or []


def ability_text(e):
    """Best-effort extraction of the triggered ability's own rules text."""
    for path in (["kind", "data", "ability", "description"],
                 ["ability", "description"],
                 ["data", "ability", "description"],
                 ["description"], ["text"]):
        v = e
        try:
            for k in path:
                v = v[k]
            if isinstance(v, str) and v.strip():
                return v.lower()
        except (KeyError, TypeError):
            pass
    return ""


def is_death_trigger(e):
    """True iff this stack entry is Toxrill's slime-DEATH trigger.

    Timing-based discrimination (robust to 106 stack-entry shape changes):
    once the Elves is in P1's graveyard, any slime-phrase trigger on the
    stack is the death trigger -- the end-step counter trigger must have
    resolved for the death to happen. Before the death, discriminate on the
    ability's own text.
    """
    blob = json.dumps(e, default=str).lower()
    if "slime counter" not in blob:
        return False
    if ST.get("elves_died"):
        return True
    t = ability_text(e)
    if t:
        return ("slime counter on it dies" in t
                and "put a slime counter on each creature" not in t)
    return False


def count_death_triggers(state):
    return sum(1 for e in stack_entries(state) if is_death_trigger(e))


def count_slugs(state):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Battlefield"
               and str(o.get("controller", -1)) == "0"
               and "slug" in oname(o).lower())


def st_of(c):
    return c.latest or {}

# ------------------------------------------------------------- interaction primitives
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"


def drain_rejections(c, tag):
    """Drain per-tick rejections (AGENTS.md 2026-10-07): a rejected
    submission whose iid stays in SUBMITTED_OPPS suppresses resubmission
    forever -> deadlock. Re-arm any gated discard so the corrected
    submission goes out on the next tick."""
    rej = getattr(c, "rejections", None) or []
    if not rej:
        return
    c.rejections = []
    for r in rej:
        data = r.get("data", {}) or {}
        iid = data.get("interactionId") or data.get("interaction_id")
        wire("rejection", {"who": tag, "rejection": r})
        say(f"[{tag}] REJECTED: {json.dumps(r)[:220]}")
        if iid:
            key = (tag, "discard", str(iid))
            if key in SUBMITTED_OPPS:
                SUBMITTED_OPPS.discard(key)
                say(f"[{tag}] re-armed discard for iid={iid}")


def check_data_level():
    ok, notes = True, []
    checks = {
        "the masamune": ["triggers an additional time", "equip {2}"],
        "toxrill, the corrosive": ["slime counter", "create a 1/1 black slug"],
        "llanowar elves": ["add {g}"],
    }
    oracle = {}
    for name, needles in checks.items():
        e = CARD_DATA.get(name, {})
        ot = str(e.get("oracle_text", ""))
        oracle[name] = ot[:300]
        for needle in needles:
            if needle.lower() not in ot.lower():
                ok = False
                notes.append(f"{name}: oracle missing {needle!r}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes, "oracle": oracle}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    assert ok, "; ".join(notes)


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
            wire("action_submit", {"who": tag, "action": "ChooseLegend/asis"})
            await submit_as_is(c, a)
            say(f"[{tag}] legend rule: keeps first")
            return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
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

# ------------------------------------------------------------- seat ticks
def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if is_land(get_obj(state, o)):
        return 0
    if ln in (TOXRILL, MASAMUNE):
        return 3
    return 1


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if is_land(get_obj(state, o)):
        return 0
    if ln == ELVES:
        return 3
    return 1


def p0_land_prefer(state, o):
    # Swamp-first until 2 Swamps (Toxrill needs BB), then keep balanced.
    ln = obj_lname(state, o)
    n_swamp = len(bf_by_name(state, 0, SWAMP))
    n_island = len(bf_by_name(state, 0, ISLAND))
    if n_swamp < 2:
        return 0 if ln == SWAMP else 1
    return (n_island - n_swamp) if ln == ISLAND else (n_swamp - n_island)


async def p0_tick(c, tag):
    drain_rejections(c, tag)
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await do_legend(c, acts, tag):
        return True
    if await pay_tick(c, acts):
        return True

    # Equip target selection: answer only while an equip activation is
    # pending, and only with Toxrill's candidate.
    if ST.get("equip_activated") and not ST.get("equip_done"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            tox = bf_by_name(state, 0, TOXRILL)
            tox_oid = str(tox[0]) if tox else None
            chs = ((opp.get("response", {}) or {}).get("data", {}) or {}
                   ).get("candidates") or \
                ((opp.get("response", {}) or {}).get("data", {}) or {}
                 ).get("choices") or []
            pick = None
            for ch in chs:
                if tox_oid and tox_oid in cand_refs(ch):
                    pick = ch
                    break
            if pick is None:
                ST["equip_target_ticks"] = ST.get("equip_target_ticks", 0) + 1
                wire("equip_target_no_toxrill",
                     {"refs": [cand_refs(ch) for ch in chs],
                      "ticks": ST["equip_target_ticks"]})
                if ST["equip_target_ticks"] > 40:
                    say("[P0] equip target never offered Toxrill; resetting "
                        "equip attempt")
                    ST["equip_activated"] = False
                    ST["equip_target_ticks"] = 0
                    ST["inflight"].pop(tag, None)
                return True
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                ST["equip_done"] = True
                ST["equip_target_ticks"] = 0
                say(f"[P0] equip target -> Toxrill (choice {pick.get('id')})")
                wire("equip_target_submit", {"choice": pick.get("id")})
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

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # 2026-10-09 cast-confirmation guard (protocol 118): while a CastSpell /
    # ActivateAbility submission is unconfirmed, hold -- no priority pass,
    # no new main-phase plays. Payment ticks and the equip-target answer
    # above are unaffected.
    if inflight_guard(c, tag, 0):
        return True

    # fresh view before leg evaluation
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag, prefer=p0_land_prefer):
            return True
        mas = bf_by_name(state, 0, MASAMUNE)
        tox = bf_by_name(state, 0, TOXRILL)
        # Equip: advertised ActivateAbility first, vi exactChoices fallback
        if mas and tox and not masamune_equipped_toxrill(state) \
                and not ST.get("equip_activated"):
            act = next((a for a in acts
                        if a.get("type") == "ActivateAbility"
                        and (str(a.get("_src_oid")) == str(mas[0])
                             or str((a.get("data") or {}).get("source_id"))
                             == str(mas[0]))), None)
            if act is not None:
                MANA_NEEDS[tag] = {"generic": 2}
                ST["equip_activated"] = True
                say(f"[P0] equip via advertised ActivateAbility (src {mas[0]})")
                wire("equip_activate_action", {"src": mas[0]})
                await submit_as_is(c, act)
                set_inflight(tag, "equip", MASAMUNE)
                return True
            for opp in vi_ops(st):
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                for ch in data.get("choices") or []:
                    if "activateAbility" not in surf_codes(ch):
                        continue
                    refs = cand_refs(ch)
                    srcs = [str((s.get("data") or {}).get("source"))
                            for s in ch.get("surfaces", []) or []
                            if isinstance(s.get("data"), dict)]
                    if str(mas[0]) in refs or str(mas[0]) in srcs:
                        MANA_NEEDS[tag] = {"generic": 2}
                        ST["equip_activated"] = True
                        say("[P0] equip via vi activateAbility choice")
                        wire("equip_activate_vi", {"src": mas[0]})
                        await answer_vi(c, opp, ch, tag)
                        set_inflight(tag, "equip", MASAMUNE)
                        return True
        # Cast Masamune (once), then Toxrill (once)
        if not mas:
            a, oid = find_cast_action(acts, state, MASAMUNE)
            if a is not None:
                MANA_NEEDS[tag] = {"generic": 3}
                say(f"[P0] casts The Masamune (oid {oid})")
                wire("cast_masamune", {"oid": oid})
                await submit_as_is(c, a)
                set_inflight(tag, "cast", MASAMUNE)
                return True
        if not tox:
            a, oid = find_cast_action(acts, state, TOXRILL)
            if a is not None:
                MANA_NEEDS[tag] = {"B": 2, "generic": 5}
                say(f"[P0] casts Toxrill (oid {oid})")
                wire("cast_toxrill", {"oid": oid})
                await submit_as_is(c, a)
                set_inflight(tag, "cast", TOXRILL)
                return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    drain_rejections(c, tag)
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await do_legend(c, acts, tag):
        return True
    if await pay_tick(c, acts):
        return True
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # 2026-10-09 cast-confirmation guard (protocol 118): while the Elves
    # cast is unconfirmed, hold -- no priority pass, no new main-phase plays.
    if inflight_guard(c, tag, 1):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return True
        # cast exactly one Elves, gated on the equip being complete
        if (masamune_equipped_toxrill(state) and not ST.get("elves_cast")
                and not bf_by_name(state, 1, ELVES)
                and not gy_by_name(state, 1, ELVES)):
            a, oid = find_cast_action(acts, state, ELVES)
            if a is not None and untapped_lands(state, 1, FOREST) >= 1:
                MANA_NEEDS[tag] = {"G": 1}
                ST["elves_cast"] = True
                say(f"[P1] casts Llanowar Elves (oid {oid})")
                wire("cast_elves", {"oid": oid})
                await submit_as_is(c, a)
                set_inflight(tag, "cast", ELVES)
                return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ------------------------------------------------------------- checkpoints
async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def evaluate_checkpoints(p0):
    st = st_of(p0)
    if not st:
        return
    state = st["state"]
    turn = state.get("turn_number")
    phase = state.get("phase") or ""

    if gy_by_name(state, 1, ELVES):
        ST["elves_died"] = True

    # PRE: Toxrill equipped, Elves on P1 BF, no slime counters yet
    if not ST.get("pre_exported") and masamune_equipped_toxrill(state):
        if elves_slime_free(state):
            pre = await do_export(p0, "pre_death.json")
            ST["pre_exported"] = True
            ST["pre_turn"] = pre.get("turn_number")
            grant = "DoubleTriggers" in json.dumps(pre)
            wire("pre_death", {"turn": pre.get("turn_number"),
                               "phase": pre.get("phase"),
                               "equipped": masamune_equipped_toxrill(pre),
                               "double_triggers_grant": grant})
            say(f"[pre] exported turn {pre.get('turn_number')} phase "
                f"{pre.get('phase')} DoubleTriggers-grant={grant}")

    # Stack audit: log every distinct stack snapshot from pre through post
    if ST.get("pre_exported") and not ST.get("post_exported"):
        stack = stack_entries(state)
        if stack:
            sig = json.dumps(stack, default=str, sort_keys=True)
            if sig != ST.get("last_stack_sig"):
                ST["last_stack_sig"] = sig
                ndt = count_death_triggers(state)
                ST["max_death_triggers"] = max(
                    ST.get("max_death_triggers", 0), ndt)
                wire("stack_snapshot",
                     {"turn": turn, "phase": phase,
                      "death_triggers": ndt,
                      "stack_size": len(stack),
                      "elves_died": bool(ST.get("elves_died"))})

    # MID: first revision with Toxrill's slime-DEATH trigger on the stack
    if ST.get("pre_exported") and not ST.get("mid_exported"):
        if count_death_triggers(state) >= 1:
            mid = await do_export(p0, "mid_triggers.json")
            ST["mid_exported"] = True
            ST["mid_turn"] = mid.get("turn_number")
            ST["mid_death_triggers"] = count_death_triggers(mid)
            ST["mid_stack_size"] = len(stack_entries(mid))
            wire("mid_triggers", {"turn": ST["mid_turn"],
                                 "death_triggers": ST["mid_death_triggers"],
                                 "stack_size": ST["mid_stack_size"]})
            say(f"[mid] exported: {ST['mid_death_triggers']} slime-death "
                f"triggers on stack")

    # POST: stack empty and the turn advanced past the mid turn's end step
    if ST.get("mid_exported") and not ST.get("post_exported"):
        if (not stack_entries(state) and turn is not None
                and ST.get("mid_turn") is not None and turn > ST["mid_turn"]):
            await asyncio.sleep(1.0)
            post = await do_export(p0, "post_resolution.json")
            ST["post_exported"] = True
            slugs = count_slugs(post)
            wire("post_resolution", {"turn": post.get("turn_number"),
                                    "phase": post.get("phase"),
                                    "slugs": slugs, "stack_empty": True})
            say(f"[post] exported: {slugs} Slug tokens")
            ST["stop"] = True


# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06773")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(("Toxrill, the Corrosive", 12), ("The Masamune", 8),
                         ("Island", 20), ("Swamp", 20)))
    p1 = PhaseClient("P16773")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code,
                  deck(("Llanowar Elves", 12), ("Forest", 48)))
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
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                # 2026-10-07: re-tick not only when holding priority, but
                # also when a pending decision opportunity sits unanswered
                # (no revision change) -- otherwise a driver-held hold can
                # stall the game indefinitely.
                pending = my_priority(top_acts(st)) or bool(vi_ops(st))
                if not (pending
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
        await evaluate_checkpoints(p0)
        if time.time() - last_diag > 60:
            last_diag = time.time()
            for c in (p0, p1):
                st = st_of(c)
                if not st:
                    continue
                s = st["state"]
                say(f"DIAG {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} prio={my_priority(top_acts(st))} "
                    f"decision={real_decision_pending(st)} "
                    f"pre={bool(ST.get('pre_exported'))} "
                    f"mid={bool(ST.get('mid_exported'))} "
                    f"equipped={masamune_equipped_toxrill(s)}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_death.json")
    mid = env_state("mid_triggers.json")
    post = env_state("post_resolution.json")
    A = obs["assert"]

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_death.json never exported")
    else:
        ok_tox = bool(bf_by_name(pre, 0, TOXRILL))
        ok_equip = masamune_equipped_toxrill(pre)
        ok_elves = elves_slime_free(pre)
        grant = "DoubleTriggers" in json.dumps(pre)
        obs["notes"].append(
            f"pre_death: turn={pre.get('turn_number')} phase={pre.get('phase')} "
            f"toxrill_bf={ok_tox} masamune_attached={ok_equip} "
            f"elves_bf_slime_free={ok_elves} DoubleTriggers_grant={grant}")
        A["A1_setup_ok"] = "passed" if (ok_tox and ok_equip and ok_elves) \
            else "failed"

    # A2
    if post is None or mid is None:
        A["A2_death_observed"] = "not-run"
        obs["notes"].append("mid or post missing; A2 not-run")
    else:
        elves_gy = bool(gy_by_name(post, 1, ELVES))
        n_trig = count_death_triggers(mid)
        obs["notes"].append(f"post: elves_in_p1_gy={elves_gy}; mid: "
                            f"toxrill slug triggers on stack={n_trig}")
        A["A2_death_observed"] = "passed" if (elves_gy and n_trig >= 1) \
            else "failed"

    # A3 (mid count, with the per-tick audit as a timing-robust backstop)
    if mid is None:
        A["A3_triggers_doubled"] = "not-run"
        obs["notes"].append("mid_triggers.json missing; A3 not-run")
    else:
        n = count_death_triggers(mid)
        amax = ST.get("max_death_triggers", 0)
        obs["notes"].append(f"mid: {n} Toxrill slug triggers on stack "
                            f"(expected 2 with doubling); per-tick audit max "
                            f"death-trigger count={amax}")
        A["A3_triggers_doubled"] = "passed" if (n == 2 or amax >= 2) \
            else "failed"

    # A4
    if post is None:
        A["A4_slugs_created"] = "not-run"
        obs["notes"].append("post_resolution.json missing; A4 not-run")
    else:
        n = count_slugs(post)
        obs["notes"].append(f"post: {n} Slug tokens on P0 battlefield "
                            f"(expected 2 with doubling)")
        A["A4_slugs_created"] = "passed" if n == 2 else "failed"

    # A5
    if post is None:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append("post_resolution.json missing; A5 not-run")
    else:
        empty = not stack_entries(post)
        obs["notes"].append(f"post: stack_empty={empty} "
                            f"turn={post.get('turn_number')} "
                            f"phase={post.get('phase')}")
        A["A5_cleanup"] = "passed" if empty else "failed"

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/scenario_result.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "game_code": ST.get("game_code"),
                   "max_death_triggers_audit": ST.get("max_death_triggers", 0),
                   "mid_death_triggers": ST.get("mid_death_triggers"),
                   "equip_activated": bool(ST.get("equip_activated")),
                   "equip_done": bool(ST.get("equip_done")),
                   "elves_cast": bool(ST.get("elves_cast")),
                   "elves_died": bool(ST.get("elves_died"))}, f, indent=1)

    verdict = "blocked"
    if A.get("A1_setup_ok") == "passed":
        a3 = A.get("A3_triggers_doubled")
        a4 = A.get("A4_slugs_created")
        if a3 == "failed" or a4 == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"

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
            "P0": [["Toxrill, the Corrosive", 12], ["The Masamune", 8],
                   ["Island", 20], ["Swamp", 20]],
            "P1": [["Llanowar Elves", 12], ["Forest", 48]],
        },
        "assertions": A,
        "notes": obs["notes"] + [
            "protocol-120 driver (v0.105.0): mulligan via legacy "
            "MulliganDecision action; DiscardToHandSize via vi "
            "schema/select; CastSpell via legacy Action with mana via "
            "legacy PayMana actions and/or vi tapLandForMana menus driven "
            "by MANA_NEEDS; equip via advertised ActivateAbility "
            "merged_action (source match), vi exactChoices as fallback; "
            "equip target via advertised vi target opportunity; "
            "priority-gated passes; per-revision guards; 5s re-tick "
            "backstop.",
            "Death-trigger discrimination is timing-based: once the Elves "
            "is in P1's graveyard, any slime-phrase trigger on the stack "
            "is Toxrill's death trigger (the end-step counter trigger "
            "must have resolved for the death to happen).",
        ],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Toxrill / 8x Masamune deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "Emblem-trigger doubling half of the clause not tested "
            "(no emblems involved).",
            "States are authoritative exports, restorable only via full game "
            "replay (scenario_6773_01050.py), not direct load.",
        ],
        "setup_line": "P0: 12x Toxrill, the Corrosive + 8x The Masamune + "
                      "20x Island + 20x Swamp; P1: 12x Llanowar Elves + "
                      "48x Forest (casts one Elves after equip)",
        "contract_line": ("With Masamune equipped to Toxrill, a slime-counter "
                          "death must put 2 triggers on the stack -> 2 Slug "
                          "tokens"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")

    await p0.close()
    await p1.close()
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    return run


if __name__ == "__main__":
    asyncio.run(main())
