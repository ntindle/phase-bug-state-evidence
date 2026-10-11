#!/usr/bin/env python3
"""Issue #7165 re-validation on v0.106.0 (protocol 126): AI put Crown of
Skemfar on my card and spends all of its mana to get it back.

Reporter (Discord): the native AI enchants an OPPONENT's creature with Crown
of Skemfar (beneficial Aura: +1/+1 per Elf the controller controls, reach),
then repeatedly spends its mana returning the Aura from its graveyard to its
hand via the {2}{G} ability.

Maintainer triage: tactical evaluation / action selection, not Oracle AST
faithfulness. Acceptance criteria:
  - opponent-controlled targets get an appropriate negative score for
    beneficial Auras;
  - the {2}{G} return activation is compared against holding mana / board
    value;
  - the attached state selects a legal non-self-defeating action.

Plan (native engine, v0.106.0 / protocol 126, native Medium AI):
  P0 (human driver): 6x Back to Nature + 16x Fog + 8x Nourish + 30x Forest.
     Plays a land a turn, never attacks, never blocks. Once the AI's Crown is
     on the battlefield, P0 casts Back to Nature ("Destroy all
     enchantments", no target selection), sending the Crown to the AI's
     graveyard; re-casts on every AI re-attach to keep the loop observable.
     P0 survives via Fog (cast during the AI's combat steps) and Nourish.
  P1 (native Medium AI): 12x Crown of Skemfar + 12x Llanowar Elves +
     36x Forest.

Behavioral contract:
  A1 setup_ok        AI casts Crown of Skemfar; the Aura is observed on the
                     battlefield (mid_attach.json exported).
  A2 correct_target  the enchanted creature is controlled by the AI (seat 1).
                     failed = the AI buffed the opponent's creature (reported).
  A3 return_observed Crown reached the AI graveyard and the AI activated the
                     {2}{G} return (graveyard->hand transition) at least
                     once. not-run if the Crown never reached the AI's
                     graveyard or the watch window was interrupted.
  A4 no_return_loop  AI activated the return at most once in the 12-turn
                     watch window. failed = >=2 return activations (the
                     "spends mana to get it back" loop). not-run if A3
                     not-run.
  A5 cleanup         no revision stall; game advanced >=3 turns past the
                     attach; final stack empty (or game over).

Verdict: reproduced iff A2 fails, or (A3 passes and A4 fails).
         not-reproduced iff A1 passes and A2 passes and the return leg was
         fully exercised (watch window closed) with A3 failed (AI never
         returned the Crown) or (A3 passed and A4 passed) (single return,
         no loop), or the Crown never reached the graveyard at all (return
         leg inapplicable; target-choice half tested).
         blocked iff the AI never casts Crown within the turn cap (A1 fails),
         or the watch window was interrupted after the Crown reached the
         graveyard (return leg not fully exercised).

Protocol-126 driver notes (v0.106.0, 2026-10-11): port of the verified
protocol-70 scenario_7165.py (run 20260914-7165l) using the conventions
established in the verified protocol-126 scenario_301_01060.py:
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action, gated on the
    MulliganDecision legal action. Keep with 2+ lands.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards.
  - Optional-cast accept/decline not needed for this fixture.
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    reading fresh state for leg evaluation (leg-engagement race fix).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
  - deck schema {"main_deck": [...], "sideboard": [], "commander": []};
    client.py HELLO advertises 126 (exact match enforced).
  - CastSpell submitted via legacy action + mana paid via vi tapLandForMana
    with a needs dict; pending_cast_oids prevents double submission.
  - All viewer_interaction opportunities are wire-logged (opportunity
    events) so an unanswered prompt leaves a trace.
  - attached_to is extracted defensively from the Aura object (data-key
    first, then any int) and the raw value is wire-logged.
  - A return activation is detected as a Graveyard->Hand zone transition of
    an AI-controlled Crown (no other graveyard recursion exists in either
    deck); each event records turn, AI hand size, and AI untapped Forests.
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
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7165-v01060-20261011-1142")
ISSUE = 7165
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CROWN = "Crown of Skemfar"
BTN = "Back to Nature"
FOG = "Fog"
NOURISH = "Nourish"
FOREST = "Forest"
ELVES = "Llanowar Elves"
P0_DECK = [(BTN, 6), (FOG, 16), (NOURISH, 8), (FOREST, 30)]
P1_AI_DECK = [(CROWN, 12), (ELVES, 12), (FOREST, 36)]
TIMEOUT = 3000
STALL_AFTER = 180
TURN_CAP = 60
WATCH_TURNS = 12  # return-loop watch window after first graveyard arrival

SERVER_IDENTITY = {
    "server_version": "v0.106.0",
    "build_commit": "29e0db3",
    "protocol_version": 126,
    "mode": "Full",
    "server_binary_sha256":
        "5d31aeec7387c22b5af84f7befa83c14f785d8505c0f9102f4fddac743d1d47a",
    "card_data_sha256":
        "59d60da9833ff7cd464fca2c93f4d6b49d74a8ad0d04092055634442c70cbc41",
    "draft_pools_sha256":
        "fad736f3ae13b3fb2f0908319eae98af501e36cc30fbe2de84ca6684319384c86",
    "signature_verified": True,
    "signature_note": ("v0.106.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; digests recomputed against on-disk files; "
                       "ServerHello 0.106.0/29e0db3/protocol 126 verified by "
                       "live handshake this run on 127.0.0.1:9374."),
}

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> ATTACHED -> RETURN_WATCH -> DONE
        "stop": False,
        "crown_seen": False, "crown_oid": None, "attach_turn": None,
        "attach_controller": None, "attach_creature_oid": None,
        "attach_creature_name": None, "attach_raw": None,
        "attach_exported": False,
        "btn_cast_turn": -1, "fog_cast_turn": -1, "nourish_cast_turn": -1,
        "gy_arrived": False, "gy_turn": None, "gy_exported": False,
        "watch_until_turn": None,
        "return_count": 0, "return_events": [],
        "stack_return_seen": set(),
        "crown_zones": {},  # oid(str) -> zone, for AI-controlled Crowns
        "stall_observed": False, "stall_wf": None,
        "last_rev_change": None, "game_started": False, "game_over": False,
        "rejections": [], "turns_seen": set(),
        "ai_mana_samples": [], "post_exported": False,
        "watch_closed": False,
        "pending_cast_oids": set(),
        "mana_needs": {},
    })
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


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


def untapped_forests(state, pid):
    return sum(1 for o in bf_by_name(state, pid, "forest")
               if not get_obj(state, o).get("tapped"))


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.106.0"), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", c.revision)
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    n_lands = sum(1 for o in hand_ids(state, pid)
                  if is_land(get_obj(state, o)))
    choice = "Keep" if n_lands >= 2 else "Mulligan"
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] mulligan -> {choice} (lands={n_lands})")
    wire("mulligan", {"who": tag, "decision": choice, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
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
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get(
        "data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == "back to nature":
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [
                                           ch.get("id") for ch in picks]}}},
                      tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    found = False
    if "discard" in code.lower():
        found = True
    else:
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec") or {}
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
        if nm in ("back to nature", "fog", "nourish"):
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
                 sorted(cands,
                        key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (
                    None, "available"):
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
                                                   "data": {
                                                       "choiceId":
                                                           ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a.get("type", "").lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, str(iv)
    return None, None


def clear_stale_pending_casts(state):
    """Drop pending_cast_oids whose card left P0's hand (resolved/fizzled)."""
    hand = set(hand_ids(state, 0))
    for oid in list(ST["pending_cast_oids"]):
        if oid not in hand:
            ST["pending_cast_oids"].discard(oid)


async def try_cast(c, acts, state, tag, card_name, needs, why, guard_turn_key):
    """Submit a CastSpell for card_name if affordable and not already
    pending; drive payment via needs."""
    if ST[guard_turn_key] == state.get("turn_number"):
        return False
    clear_stale_pending_casts(state)
    a, oid = cast_action_for(acts, state, card_name)
    if a is None or oid in ST["pending_cast_oids"]:
        return False
    n_untapped = untapped_forests(state, 0)
    cost = sum(needs.values())
    if n_untapped < cost:
        return False
    ST[guard_turn_key] = state.get("turn_number")
    ST["pending_cast_oids"].add(oid)
    ST["mana_needs"] = dict(needs)
    say(f"[{tag}] casting {card_name} (oid={oid}) {why}; needs={needs}")
    wire("cast", {"who": tag, "card": card_name, "oid": oid, "why": why,
                  "needs": needs})
    await submit_as_is(c, a)
    return True


async def p0_tick(c, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    pid = 0
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    if "DeclareAttackers" in atypes and state.get("active_player") == pid:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"]
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    # Fog the AI's combat: cast when the AI is in its combat steps and has
    # declared (or may declare) attackers; once per turn.
    phase = state.get("phase") or ""
    if state.get("active_player") == 1 and phase in (
            "DeclareAttackers", "DeclareBlockers"):
        ai_attackers = any(
            obj_lname(state, o) not in ()
            for o in bf_oids(state, 1)
            if not get_obj(state, o).get("tapped")
            and (get_obj(state, o).get("power") or 0) > 0
            and "Land" not in (
                (get_obj(state, o).get("card_types") or {}).get(
                    "core_types") or []))
        if ai_attackers and await try_cast(
                c, acts, state, tag, "fog", {"G": 1},
                f"AI combat turn={state.get('turn_number')} phase={phase}",
                "fog_cast_turn"):
            return True

    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return True
        # Back to Nature the AI's Crown: destroy all enchantments, no
        # target selection needed.
        crowns = bf_by_name(state, 1, "crown of skemfar")
        if crowns and ST["stage"] in ("ATTACHED", "RETURN_WATCH"):
            if await try_cast(
                    c, acts, state, tag, "back to nature",
                    {"G": 2, "generic": 1},
                    f"Crown on AI BF x{len(crowns)}",
                    "btn_cast_turn"):
                return True
        # Nourish for survival when life is low.
        if (life(state, pid) or 20) <= 16:
            if await try_cast(c, acts, state, tag, "nourish",
                              {"G": 1, "generic": 2},
                              f"life={life(state, pid)}",
                              "nourish_cast_turn"):
                return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        await pass_priority(c, st, acts)
        return True
    return False


OPPS_LOGGED = set()


async def log_opportunities(c):
    for opp in vi_ops(c.latest or {}):
        sig = (opp.get("interactionId"),
               (opp.get("response") or {}).get("type"))
        if sig in OPPS_LOGGED:
            continue
        OPPS_LOGGED.add(sig)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items[:8]:
            codes.update(x for x in surf_codes(ch) if x)
        wire("opportunity",
             {"iid": str(opp.get("interactionId"))[:16],
              "rtype": resp.get("type"), "n_items": len(items),
              "codes": sorted(codes), "stage": ST.get("stage")})


def attached_creature_oid(o):
    """Defensively extract the enchanted creature's oid from attached_to."""
    a = o.get("attached_to")
    if a is None:
        return None

    def find_data(x):
        if isinstance(x, dict):
            d = x.get("data")
            if isinstance(d, int) and not isinstance(d, bool):
                return d
            for v in x.values():
                r = find_data(v)
                if r is not None:
                    return r
        elif isinstance(x, list):
            for v in x:
                r = find_data(v)
                if r is not None:
                    return r
        return None

    r = find_data(a)
    if r is not None:
        return r

    def find_int(x):
        if isinstance(x, bool):
            return None
        if isinstance(x, int):
            return x
        if isinstance(x, dict):
            for v in x.values():
                q = find_int(v)
                if q is not None:
                    return q
        elif isinstance(x, list):
            for v in x:
                q = find_int(v)
                if q is not None:
                    return q
        return None

    return find_int(a)


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    # export_state returns the envelope JSON string (parse once per playbook)
    env = json.loads(s)
    with open(f"{EVDIR}/{path}", "w") as f:
        json.dump(env, f, indent=1)
    say(f"exported {path}")
    return env


def stack_return_entries(state):
    """AI-controlled abilities on the stack whose description matches the
    Crown's {2}{G} return ability."""
    hits = []
    for entry in state.get("stack") or []:
        if entry.get("controller") != 1:
            continue
        blob = json.dumps(entry, default=str).lower()
        if ("return" in blob and "graveyard" in blob and "hand" in blob
                and "this card" in blob):
            hits.append(entry)
    return hits


async def observe(state):
    turn = state.get("turn_number") or 0
    objs = state.get("objects") or {}
    ai_crowns_bf = bf_by_name(state, 1, "crown of skemfar")

    # --- attach leg ---
    if not ST["crown_seen"] and ai_crowns_bf:
        ST["crown_seen"] = True
        ST["crown_oid"] = ai_crowns_bf[0]
        ST["attach_turn"] = turn
        ST["stage"] = "ATTACHED"
        o = objs[ai_crowns_bf[0]]
        raw = o.get("attached_to")
        ST["attach_raw"] = raw
        coid = attached_creature_oid(o)
        ST["attach_creature_oid"] = coid
        cname, cctl = None, None
        if coid is not None:
            co = objs.get(str(coid)) or {}
            cname = (co.get("base_name") or co.get("name"))
            cctl = co.get("controller")
        ST["attach_creature_name"] = cname
        ST["attach_controller"] = cctl
        say(f"Crown of Skemfar on AI BF oid={ST['crown_oid']} turn={turn} "
            f"attached_to oid={coid} name={cname!r} controller={cctl}")
        wire("crown_attached", {"turn": turn, "crown_oid": ST["crown_oid"],
                                "attached_creature_oid": coid,
                                "attached_creature_name": cname,
                                "attached_controller": cctl,
                                "attached_to_raw": raw})
        if await export_now("mid_attach.json") is not None:
            ST["attach_exported"] = True

    # --- zone tracking for AI-controlled Crowns (return detection) ---
    for oid, o in objs.items():
        name = (o.get("base_name") or o.get("name") or "").lower()
        if name != "crown of skemfar" or o.get("controller") != 1:
            continue
        prev = ST["crown_zones"].get(str(oid))
        zone = o.get("zone")
        if prev == "Graveyard" and zone == "Hand":
            recent_stack = any(
                ev.get("via") == "stack" and turn - ev.get("turn", 0) <= 2
                for ev in ST["return_events"])
            if recent_stack:
                say(f"AI return resolved: Crown oid={oid} gy->hand "
                    f"turn={turn} (already counted via stack)")
                wire("ai_return_resolved",
                     {"turn": turn, "crown_oid": oid})
            else:
                ST["return_count"] += 1
                ev = {"turn": turn, "crown_oid": oid, "via": "zone",
                      "ai_untapped_forests": untapped_forests(state, 1),
                      "p0_life": life(state, 0), "p1_life": life(state, 1)}
                ST["return_events"].append(ev)
                say(f"AI RETURN ACTIVATION #{ST['return_count']}: Crown "
                    f"oid={oid} gy->hand turn={turn} "
                    f"ai_untapped_forests={ev['ai_untapped_forests']}")
                wire("ai_return", ev)
        ST["crown_zones"][str(oid)] = zone

    # --- return activation on the stack ---
    for entry in stack_return_entries(state):
        key = (turn, str(entry.get("id")))
        if key not in ST["stack_return_seen"]:
            ST["stack_return_seen"].add(key)
            ST["return_count"] += 1
            ev = {"turn": turn, "stack_id": entry.get("id"), "via": "stack",
                  "ai_untapped_forests": untapped_forests(state, 1),
                  "p0_life": life(state, 0), "p1_life": life(state, 1)}
            ST["return_events"].append(ev)
            say(f"AI RETURN ACTIVATION #{ST['return_count']}: on stack "
                f"id={entry.get('id')} turn={turn}")
            wire("ai_return", ev)

    # --- graveyard arrival leg ---
    if ST["stage"] == "ATTACHED" and not ST["gy_arrived"]:
        in_gy = [oid for oid, o in objs.items()
                 if (o.get("base_name") or o.get("name") or "").lower()
                 == "crown of skemfar"
                 and o.get("controller") == 1
                 and o.get("zone") == "Graveyard"]
        if in_gy:
            ST["gy_arrived"] = True
            ST["gy_turn"] = turn
            ST["watch_until_turn"] = turn + WATCH_TURNS
            ST["stage"] = "RETURN_WATCH"
            say(f"Crown in AI graveyard turn={turn}; watch window until "
                f"turn {ST['watch_until_turn']}")
            wire("crown_graveyard", {"turn": turn, "oids": in_gy})
            if await export_now("mid_graveyard.json") is not None:
                ST["gy_exported"] = True

    # --- mana sampling on AI main phases during the watch window ---
    if ST["stage"] == "RETURN_WATCH" and state.get("active_player") == 1 \
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"):
        key = (turn, state.get("phase"))
        if not ST["ai_mana_samples"] or ST["ai_mana_samples"][-1][0] != key:
            ST["ai_mana_samples"].append(
                (key, untapped_forests(state, 1)))

    # --- done watch ---
    if ST["stage"] == "RETURN_WATCH" and ST["watch_until_turn"] is not None:
        if turn > ST["watch_until_turn"] and not (state.get("stack") or []):
            say(f"watch window closed at turn={turn}; exporting post.json")
            if await export_now("post.json") is not None:
                ST["watch_closed"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
            return


async def main():
    reset()
    global C0
    await verify_server_hello()
    t0 = time.time()
    p0 = PhaseClient("P0-7165-126")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList", "data": ai_deck}}])
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = native AI Medium")
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})
    ST["last_rev_change"] = time.time()

    # parse check: Crown of Skemfar entry from the pinned card-data
    try:
        cd_path = f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"
        cd = json.load(open(cd_path))
        crown_entry = None
        for _k, v in cd.items():
            if isinstance(v, dict) and (
                    v.get("name") or "").lower() == "crown of skemfar":
                crown_entry = v
                break
        if crown_entry is not None:
            with open(f"{EVDIR}/parse_crown.json", "w") as f:
                json.dump(crown_entry, f, indent=1)
            say("wrote parse_crown.json")
        else:
            say("parse check: Crown of Skemfar NOT FOUND in card-data")
            wire("parse_missing", {"card": "Crown of Skemfar"})
    except Exception as e:
        say(f"parse check failed: {e}")

    last_rev = -1
    last_tick_wall = 0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        while p0.rejections:
            r = p0.rejections.pop(0)
            ST["rejections"].append(r)
            wire("rejected", {"who": p0.name, "type": r["type"],
                              "data": r["data"], "stage": ST.get("stage")})
            say(f"[{p0.name}] {r['type']}: "
                f"{json.dumps(r['data'])[:300]}")
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            try:
                await p0_tick(p0, "P0")
            except Exception as e:
                say(f"tick error: {type(e).__name__}: {e}")
            if p0.revision != last_rev:
                ST["last_rev_change"] = now
                last_rev = p0.revision
        elif p0.revision != last_rev:
            try:
                await p0_tick(p0, "P0")
            except Exception as e:
                say(f"tick error: {type(e).__name__}: {e}")
            ST["last_rev_change"] = now
            last_rev = p0.revision

        st = p0.latest
        if not st:
            continue
        await log_opportunities(p0)
        state = st["state"]
        turn = state.get("turn_number") or 0
        if turn >= 1:
            ST["game_started"] = True
            ST["turns_seen"].add(turn)
        if turn >= TURN_CAP and not ST["stop"]:
            say(f"turn cap {TURN_CAP} reached")
            ST["stop"] = True
            continue
        await observe(state)
        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["game_over"]:
            say("game over; exporting post.json before session expires")
            ST["game_over"] = True
            if not os.path.exists(f"{EVDIR}/post.json"):
                if await export_now("post.json") is not None:
                    ST["post_exported"] = True
            ST["stop"] = True
        if ST["game_started"] and not ST["stop"] \
                and now - ST["last_rev_change"] > STALL_AFTER:
            ST["stall_observed"] = True
            wf = state.get("waiting_for") or {}
            ST["stall_wf"] = {"type": wf.get("type"),
                              "player": (wf.get("data") or {}).get(
                                  "player")}
            say(f"STALL: no revision for {STALL_AFTER}s; "
                f"waiting_for={ST['stall_wf']}")
            wire("stall", ST["stall_wf"])
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

    await finish(p0)
    try:
        await p0.close()
    except Exception:
        pass


async def finish(p0):
    ass, notes = {}, []

    async def export_named(name):
        s = await export_now(f"{name}.json")
        return s is not None

    if not os.path.exists(f"{EVDIR}/post.json"):
        await export_named("post")
    states = {}
    for fn in ("mid_attach", "mid_graveyard", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as ex3:
            notes.append(f"state reload failed for {fn}.json: {ex3}")

    # ---- A1: setup ----
    if ST["crown_seen"]:
        ass["A1_setup_ok"] = "passed"
        notes.append(f"A1: Crown observed on AI BF oid={ST['crown_oid']} "
                     f"turn={ST['attach_turn']}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 failed: AI never put Crown of Skemfar on the "
                     "battlefield within the turn cap")

    # ---- A2: target choice ----
    if ST["crown_seen"]:
        c = ST["attach_controller"]
        try:
            c = int(c) if c is not None else None
        except (TypeError, ValueError):
            c = None
        if c == 1:
            ass["A2_correct_target"] = "passed"
            notes.append(f"A2: enchanted creature oid="
                         f"{ST['attach_creature_oid']} "
                         f"({ST['attach_creature_name']}) controlled by the "
                         f"AI itself (seat 1) - correct target choice")
        elif c == 0:
            ass["A2_correct_target"] = "failed"
            notes.append(f"A2 FAILED: enchanted creature oid="
                         f"{ST['attach_creature_oid']} "
                         f"({ST['attach_creature_name']}) controlled by the "
                         f"OPPONENT (seat 0) - the reported bug")
        else:
            ass["A2_correct_target"] = "failed"
            notes.append(f"A2 failed: enchanted creature controller={c} "
                         f"(unresolved)")
    else:
        ass["A2_correct_target"] = "not-run"
        notes.append("A2 not-run: no Crown attach observed")

    # ---- A3: return observed ----
    if ST["gy_arrived"]:
        if ST["watch_closed"]:
            if ST["return_count"] >= 1:
                ass["A3_return_observed"] = "passed"
                notes.append(f"A3: {ST['return_count']} AI return "
                             f"activation(s) observed: {ST['return_events']}")
            else:
                ass["A3_return_observed"] = "failed"
                notes.append(f"A3 failed: Crown reached AI graveyard turn "
                             f"{ST['gy_turn']} but the AI never activated "
                             "the {2}{G} return in the full watch window")
        else:
            ass["A3_return_observed"] = "not-run"
            notes.append("A3 not-run: watch window interrupted "
                         f"(game_over={ST['game_over']} "
                         f"stall={ST['stall_observed']} "
                         f"turns_seen={len(ST['turns_seen'])}) before the "
                         f"{WATCH_TURNS}-turn window closed")
    else:
        ass["A3_return_observed"] = "not-run"
        notes.append("A3 not-run: Crown never reached the AI's graveyard")

    # ---- A4: return loop ----
    if ST["gy_arrived"] and ST["watch_closed"]:
        if ST["return_count"] >= 2:
            ass["A4_no_return_loop"] = "failed"
            notes.append(f"A4 FAILED: AI returned the Crown "
                         f"{ST['return_count']} times in "
                         f"{WATCH_TURNS} turns - the reported mana-spend "
                         f"loop: {ST['return_events']}")
        else:
            ass["A4_no_return_loop"] = "passed"
            notes.append(f"A4: {ST['return_count']} return activation(s) in "
                         f"the window - no loop")
    else:
        ass["A4_no_return_loop"] = "not-run"
        notes.append("A4 not-run: return leg never started or watch "
                     "interrupted")

    # ---- A5: cleanup ----
    post = states.get("post")
    if ST["game_over"] and post is None:
        ok = (not ST["stall_observed"] and len(ST["turns_seen"]) >= 3)
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A5: game over (post.json unexportable: session "
                     f"invalidated at game end); stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])}")
    elif post is not None:
        ok = (not ST["stall_observed"]
              and len(ST["turns_seen"]) >= 3
              and (len(post.get("stack") or []) == 0
                   or ST["game_over"]))
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A5: stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])} "
                     f"stack_empty={len(post.get('stack') or []) == 0} "
                     f"game_over={ST['game_over']}")
    else:
        ass["A5_cleanup"] = "failed"
        notes.append("A5 failed: post.json missing")

    for k, v in ass.items():
        say(f"{k}: {v}")
    for n in notes:
        say("note:", n)

    if ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif ass["A2_correct_target"] == "failed":
        verdict = "reproduced"
    elif ass["A3_return_observed"] == "passed" \
            and ass["A4_no_return_loop"] == "failed":
        verdict = "reproduced"
    elif ass["A1_setup_ok"] == "passed" \
            and ass["A2_correct_target"] == "passed" \
            and ((ass["A3_return_observed"] == "failed")
                 or (ass["A3_return_observed"] == "passed"
                     and ass["A4_no_return_loop"] == "passed")):
        verdict = "not-reproduced"
    elif ass["A1_setup_ok"] == "passed" \
            and ass["A2_correct_target"] == "passed" \
            and ass["A4_no_return_loop"] in ("passed", "not-run") \
            and not ST["gy_arrived"]:
        verdict = "not-reproduced"
    elif ST["gy_arrived"] and not ST["watch_closed"]:
        verdict = "blocked"
    else:
        verdict = "blocked"
    say("VERDICT:", verdict)

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "AI put Crown of Skemfar on my card and spends all of its "
                 "mana to get it back",
        "validated_at": "2026-10-11",
        "server": SERVER_IDENTITY,
        "scope": "Native Medium AI target choice for the beneficial Aura "
                 "Crown of Skemfar + the {2}{G} graveyard-return activation "
                 "policy; native engine, one human-client seat + one native "
                 "AI seat",
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "driver_state": {
            "crown_oid": ST["crown_oid"],
            "attach_turn": ST["attach_turn"],
            "attach_controller": ST["attach_controller"],
            "attach_creature_oid": ST["attach_creature_oid"],
            "attach_creature_name": ST["attach_creature_name"],
            "gy_turn": ST["gy_turn"],
            "return_count": ST["return_count"],
            "return_events": ST["return_events"],
            "ai_mana_samples": ST["ai_mana_samples"][-12:],
            "turns_seen": sorted(ST["turns_seen"]),
            "rejections": ST["rejections"][:10],
        },
        "limitations": [
            "Browser UI not exercised; native engine via one human-client "
            "seat and one native Medium AI seat.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "P0 never attacks or blocks; combat interaction is minimized by "
            "fixture design (Fog/Nourish keep P0 alive).",
            "The {2}{G} return is detected as a Graveyard->Hand zone "
            "transition of an AI-controlled Crown (no other graveyard "
            "recursion exists in either deck), plus on-stack activation "
            "scans.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")
    shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7165_01060.py")
    # Shared backfill server: copy the owning run's server.log read-only as
    # a diagnostic excerpt (this run did not start the server).
    try:
        src = None
        for cand in (
                f"{BACKFILL}/runs/backfill-20261010-121157/server.log",):
            if os.path.exists(cand):
                src = cand
                break
        if src:
            shutil.copy(src, f"{EVDIR}/server.log")
            say(f"copied shared server.log from {src}")
        else:
            say("no shared server.log found; skipping")
    except Exception as ex4:
        notes.append(f"server.log copy failed: {ex4}")
        say("server.log copy failed:", ex4)
    render_png(run)
    # Close the logs BEFORE hashing: the manifest must cover the final bytes
    # of scenario_run.log / wire_log.jsonl (cf. AGENTS.md #6916 lesson), so
    # no say()/wire() calls may follow this point.
    WIRE.close()
    RUNLOG.close()

    def emit(*a):
        print(" ".join(str(x) for x in a), flush=True)

    files = ["mid_attach.json", "mid_graveyard.json", "post.json",
             "parse_crown.json", "run.json", "scenario_7165_01060.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            emit(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    emit("wrote manifest.sha256")
    for fn in ("mid_attach.json", "mid_graveyard.json", "post.json",
               "parse_crown.json", "run.json"):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            json.load(open(p))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
    for line in man:
        h, fn = line.split("  ")
        assert hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
    emit("validation: all JSON parse, PNG readable, hashes match")


def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1140
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#7165 - AI put Crown of Skemfar on my card and spends "
           "all of its mana to get it back", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.106.0 (29e0db3) protocol 126 - 2026-10-11 - "
           "native Medium AI", fill=(140, 160, 180))
    y += 28
    col = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: Enchant creature. Enchanted creature gets "
           "+1/+1 for each Elf you control", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "and has reach. {2}{G}: Return this card from your "
           "graveyard to your hand.", fill=(200, 210, 225))
    y += 34
    labels = {
        "A1_setup_ok": "GAME: AI casts Crown of Skemfar, Aura observed on "
                     "battlefield (mid_attach.json)",
        "A2_correct_target": "GAME: enchanted creature controlled by the AI "
                           "itself (REPORTED BUG: opponent's creature)",
        "A3_return_observed": "GAME: Crown reached AI graveyard; AI "
                            "activated the {2}{G} return (gy->hand)",
        "A4_no_return_loop": "GAME: at most 1 return in the 12-turn watch "
                           "window (REPORTED BUG: repeated mana spend)",
        "A5_cleanup": "GAME: no stall, game advanced, final stack empty",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v}", fill=c)
        y += 22
        d.text((52, y), lab[:104], fill=(150, 160, 175))
        y += 26
    y += 8
    ds = run.get("driver_state") or {}
    d.text((24, y), f"attach_turn={ds.get('attach_turn')} "
           f"attach_controller={ds.get('attach_controller')} "
           f"creature={ds.get('attach_creature_name')} "
           f"returns={ds.get('return_count')}",
           fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


if __name__ == "__main__":
    asyncio.run(main())
