#!/usr/bin/env python3
"""Issue #7358: Gwenna, Eyes of Gaea doesn't untap when a creature spell with
power 5 or greater is cast.

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, classifier: supported_aspect_defect,
status:confirmed): "Doesn't untap when a creature power 5 or greater enters."

Oracle text (pinned v0.99.0 card-data.json, 'gwenna, eyes of gaea'):
  "{T}: Add two mana in any combination of colors. Spend this mana only to
   cast creature spells or activate abilities of creature sources.
   Whenever you cast a creature spell with power 5 or greater, put a +1/+1
   counter on Gwenna and untap it."

Parse state (verified 2026-10-01 against pinned v0.99.0 card-data.json):
  triggers[0].mode == "SpellCast", valid_card = Creature with Power GE 5,
  trigger_zones = [Battlefield].
  execute.effect = PutCounter P1P1 target SelfRef (Gwenna - correct), with
  sub_ability = SetTapState Untap target TriggeringSource (the cast spell -
  WRONG; "untap it" refers to Gwenna). Untap aimed at the spell on the stack
  is a no-op, so the counter half works and the untap half silently does
  nothing. Matches the triage analysis exactly.

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Gwenna, Eyes of Gaea ({2}{G} 2/3) + 8x Lupine Prototype ({2} 5/5,
      no cast-relevant abilities) + 48x Forest.
  P1: 60x Forest dummy.

Drive:
  P0 casts Gwenna, then attacks P1 with her (taps her; P1 has no blockers).
  In post-combat main, P0 casts Lupine Prototype (power 5 >= 5). The
  SpellCast trigger must fire: +1/+1 counter on Gwenna AND Gwenna untaps.
  Reported bug: the counter appears but Gwenna stays tapped.

Assertions:
  A1_setup        pre.json (post-combat main, before the Lupine cast):
                  Gwenna on P0 BF, tapped=true, no counters, P0 life 20.
  A2_trigger_fired Gwenna-sourced TriggeredAbility observed on the stack
                  after the Lupine cast.
  A3_counter      post.json: Gwenna carries exactly one +1/+1 counter.
  A4_untap        post.json: Gwenna is untapped. FAIL = the reported bug.

Verdict rule: reproduced iff A1 passed and A4 failed (Gwenna still tapped
              after the trigger window - the reported outcome - whether via
              the exact parse defect or a related trigger failure);
              not-reproduced iff A1..A4 all passed;
              blocked iff A1 failed (setup could not be driven).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - DeclareAttackers data: {"attacks": [[oid, {"type":"Player","data":seat}]],
    "bands": []}.
  - After our own cast we get priority FIRST -- fall through to PassPriority.
"""
import asyncio
import copy as _copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7358b"
ISSUE = 7358
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
    "server_run_id": "runs/20261001-7355 (live v0.99.0 server on 127.0.0.1:9374, "
                     "reused per task body: already listening with pinned release)",
    "mode": "Full",
    "source": "2026-10-01: latest stable release v0.99.0 == pinned release dir; "
              "hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

GWENNA = "gwenna, eyes of gaea"
LUPINE = "lupine prototype"
FOREST = "forest"

P0_DECK = [("Gwenna, Eyes of Gaea", 4), ("Lupine Prototype", 8),
           ("Forest", 48)]
P1_DECK = [("Forest", 60)]

STARTING_LIFE = 20
SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
SUBMITTED = set()


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "gwenna_cast": False,
        "attack_done": False,
        "attack_turn": None,
        "pre_exported": False,
        "lupine_cast": False,
        "lupine_cast_turn": None,
        "saw_lupine_on_stack": False,
        "trigger_seen": False,
        "trigger_entry": None,
        "mid_exported": False,
        "post_exported": False,
        "pre_tapped": None,
        "pre_counters": None,
        "post_tapped": None,
        "post_counters": None,
        "ass": {k: "not-run" for k in ("A1_setup", "A2_trigger_fired",
                                       "A3_counter", "A4_untap")},
        "notes": [],
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state utils
def obj_name(state, oid):
    o = (state.get("objects") or {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def untapped_forests(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and str(o.get("base_name") or o.get("name") or "").lower() == FOREST]


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def gwenna_trigger_entry(state):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        low = json.dumps(e, default=str).lower()
        if "gwenna" in low and "triggered" in low:
            return e
    return None


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) if not isinstance(k, dict)
                           else k.get("name", "") for k in kws]:
            return False
    return True


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
                if lname(state, iv) == key:
                    return a
    return None


# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    await c.send_action(a)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId") or opp.get("id")
    key = (c.name, tag, str(iid))
    if key in SUBMITTED:
        return False
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
    SUBMITTED.add(key)
    await c.send_interaction(sub)
    return True


async def vi_pass_fallback(c, st):
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    await answer_vi(c, opp, ch, "pass")
                    return True
    return False


# ------------------------------------------------------------- common ticks
async def do_mulligan_p0(c, pid, tag):
    """P0 mulligans (max 2) when Gwenna is not in the opener; else keeps."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    hn = hand_lnames(state, pid)
    mulls = sum(1 for t in MULLS if t == (tag, "mulligan"))
    if GWENNA not in hn and len(hn) > 5 and mulls < 2:
        MULLS.add((tag, "mulligan"))
        say(f"[P0] mulligan #{mulls + 1} (no Gwenna in {len(hn)}-card hand)")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)} (gwenna={'yes' if GWENNA in hn else 'no'})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_mulligan_keep(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    MULLS.add(tag)
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") not in ("BottomCards",):
        wf = wf_of(state)
        if not (wf.get("type") == "BottomCards"
                and str((wf.get("data") or {}).get("player")) == str(pid)):
            return False
    n = ((pend.get("phase") or {}).get("count")) or 1 if pend else 1
    if (tag, "bottomed") in MULLS:
        return False
    hand = hand_ids(state, pid)

    def rank(o):
        nm = lname(state, o)
        if nm == FOREST:
            return 0
        if nm in (GWENNA, LUPINE):
            return 2
        return 1
    picks = [int(x) for x in sorted(hand, key=rank)[:n]]
    say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS.add((tag, "bottomed"))
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False

    def rank(o):
        nm = lname(state, o)
        if nm == FOREST:
            return 0
        if nm in (GWENNA, LUPINE):
            return 2
        return 1
    picks = [int(x) for x in sorted(hand, key=rank)[:n]]
    say(f"[{tag}] discarding to hand size: {[lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return await vi_pass_fallback(c, st)


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_p0(c, 0, "P0"):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if await pay_tick(c, acts, "P0"):
        return
    if await do_discard(c, 0, "P0"):
        return
    # P0 never blocks (P1 has no creatures anyway)
    if wtype == "DeclareBlockers":
        db = find_action(acts, "DeclareBlockers")
        if db:
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    # ---- scripted attack: tap Gwenna by attacking P1 ----
    if wtype == "DeclareAttackers" and state.get("active_player") == 0:
        da = find_action(acts, "DeclareAttackers")
        if da:
            gwen = bf_id(state, 0, GWENNA)
            if (not ST["attack_done"] and gwen is not None
                    and can_attack_now(state, gwen)
                    and LUPINE in hand_lnames(state, 0)):
                d = _copy.deepcopy(da)
                d.setdefault("data", {}).update(
                    {"attacks": [[gwen, {"type": "Player", "data": 1}]],
                     "bands": []})
                await submit_as_is(c, d)
                ST["attack_done"] = True
                ST["attack_turn"] = state.get("turn_number")
                say(f"[P0] attacking P1 with Gwenna (oid {gwen}) to tap her")
                wire("attack_gwenna", {"attacker": gwen, "defender": 1})
                return
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    in_flight = any((e.get("kind") or {}).get("type") == "Spell"
                    and e.get("controller") == 0
                    for e in stack_entries(state))
    # 1) cast Gwenna when affordable ({2}{G})
    if (not ST["gwenna_cast"] and bf_id(state, 0, GWENNA) is None
            and not in_flight
            and phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and GWENNA in hand_lnames(state, 0)
            and len(untapped_forests(state, 0)) >= 3):
        a = cast_action_for(acts, state, GWENNA)
        if a:
            say("[P0] casting Gwenna, Eyes of Gaea")
            wire("cast_gwenna", {"action": a["type"]})
            await submit_as_is(c, a)
            ST["gwenna_cast"] = True
            return
    # 2) post-combat: export PRE, then cast Lupine Prototype
    if (ST["attack_done"] and not ST["lupine_cast"] and not in_flight
            and phase == "PostCombatMain" and active == 0):
        if not ST["pre_exported"]:
            say("[P0] PRE: exporting (Gwenna tapped from attacking, "
                "about to cast Lupine Prototype)")
            pre = await c.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            pre_st = json.loads(pre)["state"]
            gwen = bf_id(pre_st, 0, GWENNA)
            go = get_obj(pre_st, gwen) if gwen is not None else {}
            ST["pre_tapped"] = bool(go.get("tapped"))
            ST["pre_counters"] = go.get("counters", {})
            ok = (gwen is not None and ST["pre_tapped"]
                  and not ST["pre_counters"]
                  and life_of(pre_st, 0) == STARTING_LIFE)
            ST["ass"]["A1_setup"] = "passed" if ok else "failed"
            ST["notes"].append(
                f"pre: gwenna_on_bf={gwen is not None}, tapped={ST['pre_tapped']}, "
                f"counters={ST['pre_counters']}, P0 life={life_of(pre_st, 0)}")
            ST["pre_exported"] = True
        if LUPINE in hand_lnames(state, 0) \
                and len(untapped_forests(state, 0)) >= 2:
            a = cast_action_for(acts, state, LUPINE)
            if a:
                say("[P0] casting Lupine Prototype (5/5) -- trigger window")
                wire("cast_lupine", {"action": a["type"]})
                await submit_as_is(c, a)
                ST["lupine_cast"] = True
                ST["lupine_cast_turn"] = state.get("turn_number")
                return
    # 3) normal play: land drop, then pass
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_keep(c, 1, "P1"):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await pay_tick(c, acts, "P1"):
        return
    if await do_discard(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da:
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = find_action(acts, "DeclareBlockers")
        if db:
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    if phase in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1:
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- observe/finish
def lupine_window_closed(state):
    """True when the Lupine cast + trigger window is over: the Lupine spell
    was observed ON the stack, and has since left it with no Gwenna trigger
    pending and combat/main done. The saw_lupine_on_stack gate prevents a
    TOCTOU premature close on a stale pre-cast client view (the cast flag is
    set synchronously while p0.latest lags the server)."""
    if not ST["lupine_cast"] or ST["post_exported"]:
        return False
    if not ST["saw_lupine_on_stack"]:
        return False
    if gwenna_trigger_entry(state) is not None:
        return False
    if any((e.get("kind") or {}).get("type") == "Spell"
           for e in stack_entries(state)):
        return False
    phase = state.get("phase")
    turn = state.get("turn_number") or 0
    return (phase in ("PostCombatMain", "End", "Cleanup")
            or turn > (ST["lupine_cast_turn"] or 0))


def note_lupine_on_stack(state):
    if ST["lupine_cast"] and not ST["saw_lupine_on_stack"]:
        if any((e.get("kind") or {}).get("type") == "Spell"
               and e.get("controller") == 0
               for e in stack_entries(state)):
            ST["saw_lupine_on_stack"] = True
            say("Lupine spell observed on the stack")
            wire("lupine_on_stack", {})


async def finish(c):
    notes = ST["notes"]
    ass = ST["ass"]
    if not ST["post_exported"]:
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    try:
        pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/pre.json") else None
        post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/post.json") else None
    except Exception as e:
        pre_st = post_st = None
        notes.append(f"state reload failed: {e}")

    # A2: trigger observed on the stack?
    if ST["trigger_seen"]:
        ass["A2_trigger_fired"] = "passed"
        notes.append("A2 passed: Gwenna TriggeredAbility observed on the "
                     "stack after the Lupine Prototype cast.")
    elif post_st is not None and ST["lupine_cast"]:
        ass["A2_trigger_fired"] = "failed"
        notes.append("A2 FAILED: Lupine resolved with NO Gwenna trigger ever "
                     "on the stack -- the ability did not fire at all.")
    else:
        notes.append("A2 not-run: Lupine cast never completed")

    # A3/A4 from post.json
    if post_st is not None:
        gwen = bf_id(post_st, 0, GWENNA)
        go = get_obj(post_st, gwen) if gwen is not None else {}
        ST["post_tapped"] = bool(go.get("tapped"))
        ST["post_counters"] = go.get("counters", {})
        n_p1p1 = 0
        if isinstance(ST["post_counters"], dict):
            for k, v in ST["post_counters"].items():
                if "P1P1" in str(k).upper().replace("+1/+1", "P1P1") \
                        or "1/1" in str(k):
                    n_p1p1 = v if isinstance(v, int) else 1
        # counters dict shape: {"P1P1": 1} expected; be liberal in counting
        if n_p1p1 == 0 and ST["post_counters"]:
            n_p1p1 = sum(v for v in ST["post_counters"].values()
                         if isinstance(v, int))
        if gwen is not None and n_p1p1 == 1:
            ass["A3_counter"] = "passed"
            notes.append(f"A3 passed: Gwenna has one +1/+1 counter "
                         f"(counters={ST['post_counters']}).")
        else:
            ass["A3_counter"] = "failed"
            notes.append(f"A3 FAILED: gwenna_on_bf={gwen is not None}, "
                         f"counters={ST['post_counters']} (expected one +1/+1).")
        if gwen is not None and not ST["post_tapped"]:
            ass["A4_untap"] = "passed"
            notes.append("A4 passed: Gwenna is untapped in post.json.")
        else:
            ass["A4_untap"] = "failed"
            notes.append(f"A4 FAILED (reported bug): Gwenna is still TAPPED "
                         f"in post.json (tapped={ST['post_tapped']}, "
                         f"counters={ST['post_counters']}).")
    else:
        for k in ("A3_counter", "A4_untap"):
            notes.append(f"{k} not-run: post.json missing")

    if ass["A1_setup"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup (A1) failed")
    elif ass["A4_untap"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: Gwenna did not untap after a "
                     "power-5+ creature spell was cast (reported outcome).")
    elif all(ass[k] == "passed" for k in ("A1_setup", "A2_trigger_fired",
                                          "A3_counter", "A4_untap")):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: counter placed AND Gwenna "
                     "untapped.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set")

    try:
        lines = open(f"{BACKFILL}/runs/20261001-7355/server.log").read().splitlines()
        excerpt = [l for l in lines
                   if "gwenna" in l.lower() or "trigger" in l.lower()]
        with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
            f.write("\n".join(excerpt[-120:]) + "\n")
        notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                     f"(of {len(lines)} total)")
    except Exception as e:
        notes.append(f"server.log excerpt failed: {e}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7358.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "trigger_seen": ST["trigger_seen"],
            "trigger_entry": ST["trigger_entry"],
            "saw_lupine_on_stack": ST["saw_lupine_on_stack"],
            "pre_tapped": ST["pre_tapped"],
            "pre_counters": ST["pre_counters"],
            "post_tapped": ST["post_tapped"],
            "post_counters": ST["post_counters"],
            "attack_turn": ST["attack_turn"],
            "lupine_cast_turn": ST["lupine_cast_turn"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Gwenna is tapped by attacking (driver choice, legal); her {T} "
            "mana ability is not exercised.",
            "8x Lupine Prototype density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 4x Gwenna, Eyes of Gaea + 8x Lupine Prototype "
                      "({2} 5/5) + 48x Forest; P1: 60x Forest",
        "contract_line": "Cast a power-5+ creature spell -> Gwenna gets a "
                         "+1/+1 counter AND untaps",
        "prior_runs": [],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return verdict


# ------------------------------------------------------------- main
async def main():
    reset()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0

    while time.time() - t_start < SETUP_DEADLINE_S:
        await asyncio.sleep(0.15)
        now = time.time()
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last.get(tag))
            stale = now - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = c.revision
            last_tick_at[tag] = now
            # stack observation: Gwenna trigger during the Lupine window
            try:
                note_lupine_on_stack(st["state"])
                hit = gwenna_trigger_entry(st["state"])
                if hit and ST["lupine_cast"] and not ST["mid_exported"] \
                        and not ST["trigger_seen"]:
                    ST["trigger_seen"] = True
                    ST["trigger_entry"] = json.dumps(hit, default=str)[:2000]
                    say("GWENNA TRIGGER observed on stack (Lupine cast)")
                    wire("gwenna_trigger_on_stack", hit)
                    try:
                        mid = await c.export_state()
                        with open(f"{EVDIR}/mid_trigger.json", "w") as f:
                            f.write(mid)
                        ST["mid_exported"] = True
                        say("exported mid_trigger.json")
                    except Exception as e:
                        ST["notes"].append(f"mid export failed: {e}")
            except Exception as e:
                wire("observe_error", {"err": str(e)})
            try:
                await tick(st, merged_actions(st), st["state"], c)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        if p0.latest and ST["lupine_cast"] and not ST["post_exported"] \
                and lupine_window_closed(p0.latest["state"]):
            say("Lupine window closed; exporting post and finishing")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
            except Exception as e:
                ST["notes"].append(f"post export failed: {e}")
            await finish(p0)
            await p0.close()
            await p1.close()
            return
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"gwenna={bf_ids(s, 0, GWENNA)} lupine={bf_ids(s, 0, LUPINE)} "
                f"life={[life_of(s, i) for i in (0, 1)]} "
                f"atk={ST['attack_done']} lupcast={ST['lupine_cast']} "
                f"trig={ST['trigger_seen']}")
    ST["notes"].append(f"global deadline ({SETUP_DEADLINE_S}s) hit")
    await finish(p0)
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
