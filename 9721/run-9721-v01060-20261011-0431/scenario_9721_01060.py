#!/usr/bin/env python3
"""Issue #9721 on pinned v0.106.0 (build 29e0db3, WS protocol 126):

"Storm may count later no-prompt copy casts in the same batch
 (Mizzix's Mastery -> Empty the Warrens -> Dark Ritual)"

Source-predicted engine defect (filed by matthewevans, 2026-10-09);
runtime reproduction has NOT been run before this scenario.

2026-10-11 04:11 port (scenario_9721_01060.py): the 02:41 driver never
reached its probe point - no Mizzix's Mastery was drawn in ~50 draws
(all other gates passed: 35 lands, gy ready, 0 casts). This port keeps
every interaction convention and adds: (a) P0 mulligans until Mastery
is in the opening hand (cap 6 mulls; bottoming already protects
Mastery), (b) a develop-stage turn cap (turn 100) that yields a
blocked verdict instead of spinning forever.

Printed reproduction: start the turn with no spells cast. Cast overloaded
Mizzix's Mastery ({5}{R}{R}{R}) with Empty the Warrens and Dark Ritual in
the graveyard. Choose to cast both copies in Warrens -> Ritual order, then
resolve the spells and the Storm trigger.

Expected under CR 702.40a: Mastery is the only other spell cast before
Warrens, so Storm makes ONE copy; original + copy = FOUR Goblins.
Source predicts the bug: the cast ledger already contains
[Mastery, Warrens, Ritual] when storm_copy_count_before_cast runs, so Storm
makes TWO copies -> SIX Goblins, wrongly counting Ritual (cast later).

The reverse Ritual -> Warrens order legitimately makes two Storm copies /
six Goblins (control).

Two games (native engine, two human-client seats):
  Game 1: cast order preference Warrens -> Ritual (the reported bug order).
  Game 2: cast order preference Ritual -> Warrens (legitimate control).
  P0: 4x Mizzix's Mastery + 4x Empty the Warrens + 4x Dark Ritual +
      8x Faithless Looting + 20x Mountain + 24x Island (64).
      Bins Warrens+Ritual via Faithless Looting / hand-size discard,
      develops to 8 lands (3+ Mountains), then casts overloaded Mastery
      on a turn with no prior casts.
  P1: 60x Island. Land per turn, passes priority, never attacks.

Probe: P0 casts overloaded Mastery with exactly 1x Warrens + 1x Ritual in
the graveyard, 0 spells cast earlier that turn. Both may-cast copy prompts
answered ACCEPT. Order recorded from the prompts (and verified against the
post-cast stack). Storm trigger watched; Goblin tokens counted at the end.

Assertions (per game):
  A1_setup_ok     probe point reached: 1x Warrens + 1x Ritual in gy, Mastery
                  in hand, 8+ lands (3+ Mountains), 0 prior casts this turn.
  A2_mastery_overloaded  overloaded Mastery was cast (cost {5}{R}{R}{R})
                  and resolved (Mastery exiled).
  A3_copies_cast  both copy-cast prompts answered accept; actual cast order
                  recorded.
  A4_order_warrens_first (game 1) actual order was Warrens -> Ritual.
  A5_storm_copies Storm copies == expected (game1: 1, game2: 2),
                  measured as goblins/2 - 1.
  A6_goblins      Goblin tokens == expected (game1: 4, game2: 6).

Verdict rule:
  reproduced iff a Warrens->Ritual game yields 6 Goblins / 2 Storm copies
  (the predicted miscount).
  not-reproduced iff a Warrens->Ritual game completes with 4 Goblins /
  1 Storm copy (correct CR 702.40a behavior).
  blocked iff the Warrens->Ritual path cannot be exercised.

Protocol-126 conventions (from scenario_6690_01060.py):
  - waiting_for is gone (null); priority = PassPriority in merged actions.
  - MulliganDecision via legacy Action; bottom via vi schema/select.
  - Discards via vi schema/select whose candidates reference hand cards.
  - Mana via legacy PayMana + vi tapLandForMana menus (ST mana_needs).
  - Cast-confirmation guard: after CastSpell, hold until confirmed.
  - may-cast copies: exactChoices decideOptionalEffect, answer via
    {"type":"choose","data":{"choiceId":...}} (cf. scenario_301_01060.py).
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

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 9721
RUN_ID = os.environ.get("RUN_ID", "run-9721-v01060-20261011-0411")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (stale RUN_ID reuse)"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MASTERY = "mizzix's mastery"
WARRENS = "empty the warrens"
RITUAL = "dark ritual"
LOOTING = "faithless looting"

P0_DECK = deck(("Mizzix's Mastery", 4), ("Empty the Warrens", 4),
               ("Dark Ritual", 4), ("Faithless Looting", 8),
               ("Mountain", 20), ("Island", 24))
P1_DECK = deck(("Island", 60))

ST = {"mana_needs": {}}
SUBMITTED_OPPS = set()
PASSED_REV = {}
LAND_PLAYED_THIS_ACTIVE = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return oname(get_obj(state, oid))


def players(state):
    return state.get("players") or []


def hand_ids(state, pid):
    ps = players(state)
    if pid >= len(ps):
        return []
    return [str(x) for x in ps[pid].get("hand", [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def zone_objs(state):
    return (state.get("objects") or {}).values()


def gy_names(state, pid):
    ps = players(state)
    if pid >= len(ps):
        return []
    return [oname(get_obj(state, oid)) for oid in ps[pid].get("graveyard", [])]


def gy_count(state, pid, name):
    return sum(1 for n in gy_names(state, pid) if n == name)


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def exile_names(state, pid=None):
    out = []
    for o in zone_objs(state):
        if o.get("zone") == "Exile" and (pid is None or o.get("owner") == pid
                                         or o.get("controller") == pid):
            out.append(oname(o))
    return out


def is_land_name(nm):
    return nm in ("forest", "mountain", "swamp", "island", "plains")


def is_land_obj(o):
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t or is_land_name(oname(o))


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if not o.get("tapped") and is_land_obj(o)]


def n_mountains_bf(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == "mountain")


def can_pay(state, pid, needs):
    red = sum(1 for _, o in untapped_lands(state, pid)
              if oname(o) == "mountain")
    total = len(untapped_lands(state, pid))
    if red < needs.get("R", 0):
        return False
    colored = sum(n for c, n in needs.items() if c != "generic")
    return total - colored >= needs.get("generic", 0)


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


def stack_entry_name(state, e):
    """Resolve a stack entry's card name via source_id -> objects."""
    if not isinstance(e, dict):
        return ""
    sid = e.get("source_id")
    if sid is not None:
        o = get_obj(state, sid)
        nm = (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()
        if nm:
            return nm
    return (e.get("card_name") or e.get("name") or "").lower()


def stack_entry_desc(e):
    if not isinstance(e, dict):
        return ""
    kind = e.get("kind") or {}
    if isinstance(kind, dict):
        ab = kind.get("ability")
        if isinstance(ab, dict) and ab.get("description"):
            return str(ab["description"])[:80]
        kd = kind.get("data") or {}
        if isinstance(kd, dict) and kd.get("description"):
            return str(kd["description"])[:80]
    return ""


def stack_summary(state):
    out = []
    for e in stack_entries(state):
        if isinstance(e, dict):
            kind = e.get("kind") or {}
            ktype = kind.get("type") if isinstance(kind, dict) else kind
            out.append((stack_entry_name(state, e), str(ktype),
                        stack_entry_desc(e)))
        else:
            out.append((str(e)[:60], "", ""))
    return out


def watch_copy_casts(state, G, tag):
    """Record when each may-cast copy first appears on the stack.
    The earlier appearance = earlier cast (determines actual order)."""
    names = set()
    for e in stack_entries(state):
        if isinstance(e, dict):
            kind = e.get("kind") or {}
            ktype = kind.get("type") if isinstance(kind, dict) else ""
            if ktype == "TriggeredAbility":
                continue
            names.add(stack_entry_name(state, e))
    now = time.time()
    if WARRENS in names and G.get("warrens_seen_at") is None:
        G["warrens_seen_at"] = now
        say(f"[{tag}] Warrens copy observed on stack")
        wire("copy_seen", {"card": WARRENS, "game": G["game_idx"]})
    if RITUAL in names and G.get("ritual_seen_at") is None:
        G["ritual_seen_at"] = now
        say(f"[{tag}] Ritual copy observed on stack")
        wire("copy_seen", {"card": RITUAL, "game": G["game_idx"]})
    if (G.get("warrens_seen_at") and G.get("ritual_seen_at")
            and not G.get("actual_order")):
        if G["warrens_seen_at"] < G["ritual_seen_at"]:
            G["actual_order"] = WARRENS
        elif G["ritual_seen_at"] < G["warrens_seen_at"]:
            G["actual_order"] = RITUAL
        else:
            G["actual_order"] = "simultaneous"
        say(f"[{tag}] actual copy-cast order: {G['actual_order']}")
        wire("actual_order", {"order": G["actual_order"],
                              "game": G["game_idx"]})


def goblin_tokens(state, pid=0):
    out = []
    for oid, o in bf(state, pid):
        nm = oname(o)
        if "goblin" in nm:
            out.append((oid, o))
    return out


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if obj_lname(state, oid) == name:
            return oid
    return None


def mastery_obj(state, pid=0, zone=None):
    for o in zone_objs(state):
        if oname(o) == MASTERY and (zone is None or o.get("zone") == zone):
            if o.get("controller") == pid or o.get("owner") == pid:
                return o
    return None


def cast_confirmed(state, G):
    oid = G.get("cast_pending_oid")
    if oid is not None:
        o = get_obj(state, oid)
        if o and o.get("zone") not in ("Hand", "Library"):
            return True
    name = (G.get("cast_pending_name") or "").lower()
    if name:
        blob = json.dumps(stack_entries(state)).lower()
        if name in blob:
            return True
    return False


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and not stack_entries(state))


# ------------------------------------------------------------- 106 primitives
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


def surf_codes(ch):
    return [c for c in (
        s.get("data", {}).get("code") if isinstance(s.get("data"), dict) else None
        for s in ch.get("surfaces", []) or []) if c]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "playLand", "candidate",
                      "mana", "mulliganDecision"}


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
            codes.update(surf_codes(ch))
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("text")):
                t = d.get("name") or d.get("text")
                break
    return str(t)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = {k: v for k, v in a["data"].items()
                       if not k.startswith("_")}
    msg = {k: v for k, v in msg.items() if not k.startswith("_")}
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert "0.106.0" in str(ver), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"
    assert str(build) == "29e0db3", f"unexpected build {build}"


# ------------------------------------------------------------- mulligan/bottom
async def do_mulligan(c, acts, st, pid, tag, G):
    if "MulliganDecision" not in [a.get("type") for a in acts]:
        return False
    key = (tag, "mulligan", str(c.revision), G["game_idx"])
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    mulls = G.get(f"mulls{pid}", 0)
    hn = hand_names(st["state"], pid)
    lands = sum(1 for n in hn if is_land_name(n))
    has_mastery = (MASTERY in hn) if pid == 0 else True
    cap = 6 if pid == 0 else 3
    want = (lands >= 2 and has_mastery) if pid == 0 else (lands >= 2)
    decision = "keep" if (want or mulls >= cap) else "mulligan"
    if decision == "mulligan":
        G[f"mulls{pid}"] = mulls + 1
    say(f"[{tag}] mulligan: {decision} (mulls={mulls}, lands={lands}, "
        f"mastery={MASTERY in hn if pid == 0 else 'n/a'})")
    wire("mulligan", {"who": tag, "decision": decision, "game": G["game_idx"]})
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": "Keep" if decision == "keep"
                                               else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, G):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (turn_of(state) == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
        return False
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
        key = (tag, "bottom", str(iid), G["game_idx"])
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            oid = _cand_reference(ch)
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm == MASTERY:
                return (2, str(oid))
            if nm in (WARRENS, RITUAL):
                return (1, str(oid))
            return (0, str(oid))

        ranked = sorted(cands, key=bkey)
        picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------- discard
def rank_discard(state, oid, G):
    nm = obj_lname(state, oid)
    gyw = gy_count(state, 0, WARRENS)
    gyr = gy_count(state, 0, RITUAL)
    if G.get("looting_resolving"):
        # Faithless Looting resolution: bin exactly the missing pieces.
        if nm == WARRENS and gyw == 0:
            return (0, nm)
        if nm == RITUAL and gyr == 0:
            return (0, nm)
        if nm == MASTERY:
            return (9, nm)
        if is_land_name(nm):
            return (3, nm)
        return (5, nm)
    # hand-size cleanup: bin Warrens/Ritual only while the gy lacks them.
    # Protect Faithless Looting until binning is complete (we need it to
    # bin the missing pieces).
    if nm == WARRENS and gyw == 0:
        return (0, nm)
    if nm == RITUAL and gyr == 0:
        return (0, nm)
    if nm == LOOTING:
        return (8, nm) if (gyw == 0 or gyr == 0) else (1, nm)
    if nm in (WARRENS, RITUAL):
        # gy already has one: avoid binning a duplicate (would break the
        # exactly-1+1 probe); discard only if nothing better exists.
        return (7, nm)
    if is_land_name(nm):
        return (3, nm)
    if nm == MASTERY:
        return (9, nm)
    return (4, nm)


async def do_discard(c, st, pid, tag, G):
    state = st["state"]
    hand = hand_ids(state, pid)
    handset = set(hand)
    looting = G.get("looting_resolving", False)
    if looting:
        want_n = 2
    else:
        want_n = len(hand) - 7
        if want_n <= 0:
            return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid), G["game_idx"])
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type") or ""
        if stype not in ("select", "sequence"):
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or not set(refs) <= handset:
            continue
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or want_n)
        n = min(n, len(cands))
        if n <= 0:
            continue
        if looting:
            # Faithless Looting: bin AT MOST one Warrens and one Ritual
            # (only while the graveyard lacks them); never bin duplicates.
            # Fill remaining slots with the lowest-priority cards.
            picks = []
            gyw = gy_count(state, 0, WARRENS)
            gyr = gy_count(state, 0, RITUAL)
            by_name = {}
            for ch in cands:
                oid = _cand_reference(ch)
                nm = obj_lname(state, oid) if oid is not None else "?"
                by_name.setdefault(nm, []).append(ch)
            if gyw == 0 and WARRENS in by_name:
                picks.append(by_name[WARRENS][0])
            if gyr == 0 and RITUAL in by_name:
                picks.append(by_name[RITUAL][0])
            if len(picks) < n:
                # filler: NEVER another Warrens/Ritual (we bin at most one
                # of each per Looting); duplicates stay in hand.
                def is_wr(ch):
                    oid = _cand_reference(ch)
                    nm = obj_lname(state, oid) if oid is not None else "?"
                    return nm in (WARRENS, RITUAL)
                rest = [ch for ch in cands
                        if ch not in picks and not is_wr(ch)]
                if len(rest) < n - len(picks):
                    # degenerate: hand is (almost) all W/R; allow filler
                    # from anything not already picked
                    rest = [ch for ch in cands if ch not in picks]
                rest_sorted = sorted(
                    rest, key=lambda ch: rank_discard(state, _cand_reference(ch), G))
                picks.extend(rest_sorted[:n - len(picks)])
            picks = picks[:n]
        else:
            ranked = sorted(cands, key=lambda ch: rank_discard(state, _cand_reference(ch), G))
            picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        names = [obj_lname(state, _cand_reference(x)) for x in picks]
        note = "looting" if looting else "cleanup"
        say(f"[{tag}] discard({note}): {names}")
        wire("discard_answered", {"tag": tag, "note": note, "game": G["game_idx"],
                                  "picks": names, "stype": stype})
        if looting:
            G["looting_resolving"] = False
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data": {
                                  "choiceIds": [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------- mana / priority
async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    if not needs or sum(needs.values()) <= 0:
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, [str(x) for x in syms]))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if (tag, "tapi", str(iid)) in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        for ch, syms in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in syms:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add((tag, "tapi", str(iid)))
        say(f"[{tag}] tap land for mana used_for={used} remaining={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick.get("id")}}},
                          tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, G):
    turn = turn_of(state)
    key = (tag, G["game_idx"])
    if LAND_PLAYED_THIS_ACTIVE.get(key) == (state.get("active_player"), turn):
        return False
    for o in hand_ids(state, pid):
        if is_land_obj(get_obj(state, o)):
            for a in acts:
                if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_THIS_ACTIVE[key] = (state.get("active_player"), turn)
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o, "game": G["game_idx"]})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_actions_for(acts, state, key):
    out = []
    for a in acts:
        if a.get("type") == "CastSpell":
            d = a.get("data", {})
            oid = d.get("object_id")
            try:
                if oid is not None and obj_lname(state, int(oid)) == key:
                    out.append(a)
            except (TypeError, ValueError):
                continue
    return out


def drain_rejections(c, G):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "game": G.get("game_idx")})
            say(f"[{c.name}] {t}: {body[:300]}")
            G.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": body[:500]})


# ------------------------------------------------------------- may-cast copies


def _cand_zone_and_name(ch):
    """(zone, name) for an object-surface candidate."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and s.get("type") == "object":
            return d.get("zone"), (d.get("name") or "").lower()
    return None, None


def find_maycast_select_opp(st):
    """Mizzix's Mastery may-cast: a schema select whose candidates are the
    EXILED cards (zone == 'exile'), including Warrens and Ritual.

    Zone-gating is critical: Faithless Looting copies (if selected) produce
    hand-discard prompts whose candidates are in zone 'hand' -- those must
    NEVER match here.
    """
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") not in ("select", "sequence"):
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        zones = set()
        names = []
        for ch in cands:
            z, nm = _cand_zone_and_name(ch)
            zones.add(z)
            if nm:
                names.append(nm)
        if zones != {"exile"}:
            continue
        if WARRENS in names and RITUAL in names:
            return opp
    return None


async def answer_maycast_select(c, opp, state, tag, G):
    """Select EXACTLY Warrens + Ritual (in preferred order) from the exiled
    may-cast candidates. Lootings (bin pollution) are never selected."""
    iid = opp.get("interactionId")
    key = (tag, "maycast", str(iid), G["game_idx"])
    if key in SUBMITTED_OPPS:
        return False
    rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    stype = spec.get("type") or "select"
    cands = rdata.get("candidates") or []
    want = ([WARRENS, RITUAL] if G.get("order_pref") == "warrens_first"
            else [RITUAL, WARRENS])
    picks = []
    for wname in want:
        for ch in cands:
            if ch in picks:
                continue
            _, nm = _cand_zone_and_name(ch)
            if nm == wname:
                picks.append(ch)
                break
    if len(picks) != 2:
        say(f"[{tag}] may-cast: W+R not both found in exile candidates")
        wire("maycast_missing", {"game": G["game_idx"], "opp": opp})
        return False
    SUBMITTED_OPPS.add(key)
    # selection order; the ACTUAL cast order is verified from the stack
    # (Storm trigger position relative to the copies).
    G["maycast_selected"] = list(want)
    G["maycast_submitted_at"] = time.time()
    say(f"[{tag}] may-cast select: {[ _cand_zone_and_name(x)[1] for x in picks ]} "
        f"(pref={G.get('order_pref')}; Lootings excluded)")
    wire("maycast_answered", {"tag": tag, "pref": G.get("order_pref"),
                              "picks": [_cand_zone_and_name(x)[1] for x in picks],
                              "game": G["game_idx"], "opp": opp})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": stype, "data": {
                              "choiceIds": [ch.get("id") for ch in picks]}}},
                      tag)
    return True


# ------------------------------------------------------------- per-tick handlers
async def p1_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 1, tag, G):
        return
    if await do_bottom(c, acts, st, 1, tag, G):
        return
    if await do_discard(c, st, 1, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if is_my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag, G):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


def mastery_probe_ready(state, G):
    """All probe-point conditions for the overloaded Mastery cast.

    The graveyard needs AT LEAST one Warrens and one Ritual; the may-cast
    multi-select chooses exactly one of each (duplicates stay in exile).
    """
    if gy_count(state, 0, WARRENS) < 1 or gy_count(state, 0, RITUAL) < 1:
        return False
    if find_hand(state, 0, MASTERY) is None:
        return False
    lands = [o for _, o in bf(state, 0) if is_land_obj(o)]
    if len(lands) < 8 or n_mountains_bf(state, 0) < 3:
        return False
    if G.get("casts_this_turn", 0) != 0:
        return False
    return True


async def develop_tick(c, G, tag, st, state, acts):
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_tick(c, acts):
            return
        if await pay_mana_vi(c, st, tag, needs):
            return
    if is_my_main(state, 0) and my_priority(acts):
        if G.get("max_turn", 0) > 100 and G.get("mastery_cast_turn") is None:
            say(f"[{tag}] develop turn cap reached (turn {G['max_turn']}); "
                "probe point never opened")
            wire("blocked", {"reason": "develop turn cap (100) reached; "
                              "probe point never opened",
                             "game": G["game_idx"]})
            G["blocked"] = ("develop turn cap reached: probe point never "
                             "opened (Mastery draw, lands, gy, or casts gate)")
            G["done"] = True
            return
        # --- probe point: cast overloaded Mizzix's Mastery
        if mastery_probe_ready(state, G):
            mc = cast_actions_for(acts, state, MASTERY)
            say(f"[{tag}] PROBE POINT t{turn_of(state)}: mastery cast actions "
                f"offered={len(mc)}; gy warrens={gy_count(state,0,WARRENS)} "
                f"ritual={gy_count(state,0,RITUAL)}; "
                f"lands={len([o for _,o in bf(state,0) if is_land_obj(o)])} "
                f"mountains={n_mountains_bf(state,0)}")
            wire("probe_point", {"turn": turn_of(state), "game": G["game_idx"],
                                 "cast_actions": [a.get("data") for a in mc]})
            pre = await export_now(f"pre_mastery_g{G['game_idx']}.json", c)
            G["pre_mastery"] = pre
            # pick the overload action: prefer the one whose data carries
            # the overload cost ({5}{R}{R}{R}); log all candidates.
            chosen = None
            for a in mc:
                blob = json.dumps(a.get("data", {})).lower()
                if "overload" in blob:
                    chosen = a
                    break
            if chosen is None and len(mc) > 1:
                # pick the max-cost candidate (overload 8 > normal 4)
                def cost_size(a):
                    return len(json.dumps(a.get("data", {})))
                chosen = max(mc, key=cost_size)
                say(f"[{tag}] multiple cast actions, no 'overload' marker; "
                    f"picked largest-data candidate")
            elif chosen is None:
                chosen = mc[0] if mc else None
            if chosen is None:
                say(f"[{tag}] NO CastSpell offered for Mastery at probe point")
                wire("mastery_not_offered", {"game": G["game_idx"]})
                G["blocked"] = "Mastery CastSpell not offered at probe point"
                G["done"] = True
                return
            G["mastery_cast_data"] = {k: v for k, v in chosen.get("data", {}).items()
                                      if not k.startswith("_")}
            ST["mana_needs"][tag] = {"R": 3, "generic": 5}
            G["casts_this_turn"] = G.get("casts_this_turn", 0) + 1
            await submit_as_is(c, chosen)
            G["cast_pending"] = True
            G["cast_pending_name"] = MASTERY
            d = chosen.get("data", {})
            try:
                G["cast_pending_oid"] = int(d.get("object_id"))
            except (TypeError, ValueError):
                G["cast_pending_oid"] = None
            G["cast_pending_at"] = time.time()
            G["stage"] = "mastery_flow"
            G["mastery_cast_turn"] = turn_of(state)
            say(f"[{tag}] casts Mizzix's Mastery (overload attempt) "
                f"t{turn_of(state)}")
            wire("cast", {"card": MASTERY, "mode": "overload",
                          "game": G["game_idx"]})
            return
        # --- bin Warrens/Ritual via Faithless Looting
        if not G.get("looting_resolving"):
            loid = find_hand(state, 0, LOOTING)
            hn = hand_names(state, 0)
            missing = ((gy_count(state, 0, WARRENS) == 0 and WARRENS in hn)
                       or (gy_count(state, 0, RITUAL) == 0 and RITUAL in hn))
            if loid is not None and missing and can_pay(state, 0, {"R": 1}):
                a, oid = None, None
                for ca in cast_actions_for(acts, state, LOOTING):
                    a, oid = ca, ca.get("data", {}).get("object_id")
                    break
                if a is not None:
                    ST["mana_needs"][tag] = {"R": 1, "generic": 0}
                    G["looting_resolving"] = True
                    G["casts_this_turn"] = G.get("casts_this_turn", 0) + 1
                    await submit_as_is(c, a)
                    G["cast_pending"] = True
                    G["cast_pending_name"] = LOOTING
                    try:
                        G["cast_pending_oid"] = int(oid)
                    except (TypeError, ValueError):
                        G["cast_pending_oid"] = None
                    G["cast_pending_at"] = time.time()
                    say(f"[{tag}] casts Faithless Looting (t{turn_of(state)})")
                    wire("cast", {"card": LOOTING, "game": G["game_idx"]})
                    return
        if await play_a_land(c, state, 0, acts, tag, G):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


def find_cast_mode_opp(st):
    """A follow-up prompt choosing the cast mode (overload vs normal)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        blob = json.dumps(resp).lower()
        if "overload" in blob:
            return opp
    return None


async def answer_cast_mode(c, opp, tag, G):
    iid = opp.get("interactionId")
    key = (tag, "castmode", str(iid), G["game_idx"])
    if key in SUBMITTED_OPPS:
        return True
    pick_id = None
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        if "overload" in choice_text(ch).lower():
            pick_id = ch["id"]
            break
    if pick_id is None:
        say(f"[{tag}] cast-mode prompt without an overload choice; logging")
        wire("castmode_no_overload", {"opp": opp, "game": G["game_idx"]})
        return False
    SUBMITTED_OPPS.add(key)
    G["overload_mode_chosen"] = True
    say(f"[{tag}] cast-mode: chose OVERLOAD")
    wire("castmode_answered", {"tag": tag, "mode": "overload",
                               "game": G["game_idx"]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": pick_id}}},
                      tag)
    return True


def choose_alt_cast_actions(acts, state, G):
    """ChooseAlternativeCast actions for the in-flight Mastery cast.

    The action data shape is not documented; take ALL ChooseAlternativeCast
    actions while a Mastery cast is pending (it is the only pending cast),
    and let the picker choose the overload alternative. Full data is
    wire-logged by the caller.
    """
    return [a for a in acts if a.get("type") == "ChooseAlternativeCast"]


def pick_overload_action(alts):
    """Pick the overload alternative.

    Observed shape (v0.106.0): {"choice": {"type": "Normal"}} vs
    {"choice": {"type": "Alternative"}}. Prefer an 'overload' marker, then
    an "Alternative" choice type, then the highest generic cost, then last.
    """
    for a in alts:
        if "overload" in json.dumps(a.get("data", {})).lower():
            return a, "marker"
    for a in alts:
        choice = (a.get("data", {}) or {}).get("choice", {})
        if isinstance(choice, dict) and choice.get("type") == "Alternative":
            return a, "alternative-type"
    def generic(a):
        d = a.get("data", {})
        blob = json.dumps(d)
        import re
        ms = re.findall(r'"generic"\s*:\s*(\d+)', blob)
        return max([int(m) for m in ms] or [0])
    if alts:
        best = max(alts, key=generic)
        if generic(best) > 0:
            return best, f"generic={generic(best)}"
        return alts[-1], "last-fallback"
    return None, "none"


async def mastery_flow_tick(c, G, tag, st, state, acts):
    # overload mode choice (may follow the CastSpell submission)
    mopp = find_cast_mode_opp(st)
    if mopp is not None:
        await answer_cast_mode(c, mopp, tag, G)
        return
    # ChooseAlternativeCast: engine asks normal vs overload after CastSpell
    if G.get("cast_pending") and G.get("cast_pending_name") == MASTERY:
        alts = choose_alt_cast_actions(acts, state, G)
        if alts:
            last = G.get("alt_cast_last_submit", 0)
            if time.time() - last > 5:
                G["alt_cast_last_submit"] = time.time()
                wire("alt_cast_options", {"game": G["game_idx"],
                                          "options": [a.get("data") for a in alts]})
                say(f"[{tag}] ChooseAlternativeCast options: "
                    f"{json.dumps([a.get('data') for a in alts])[:500]}")
                chosen, why = pick_overload_action(alts)
                G["alt_cast_choice_why"] = why
                if why in ("marker", "alternative-type") or why.startswith("generic="):
                    G["overload_mode_chosen"] = True
                say(f"[{tag}] choosing alternative ({why})")
                wire("alt_cast_chosen", {"why": why,
                                        "data": chosen.get("data") if chosen else None,
                                        "game": G["game_idx"]})
                if chosen is not None:
                    await submit_as_is(c, chosen)
            return
    # cast-confirmation guard
    if G.get("cast_pending"):
        needs = ST["mana_needs"].get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_tick(c, acts):
                return
            if await pay_mana_vi(c, st, tag, needs):
                return
        if cast_confirmed(state, G):
            say(f"[{tag}] cast {G['cast_pending_name']} confirmed; resuming")
            wire("cast_confirmed", {"name": G["cast_pending_name"],
                                   "game": G["game_idx"]})
            G["cast_pending"] = False
            ST["mana_needs"][tag] = {}
            if G["cast_pending_name"] == MASTERY:
                G["mastery_on_stack"] = True
        elif time.time() - G.get("cast_pending_at", 0) > 45:
            say(f"[{tag}] cast-confirmation backstop: "
                f"{G['cast_pending_name']} unconfirmed 45s")
            wire("cast_backstop", {"name": G["cast_pending_name"],
                                  "game": G["game_idx"]})
            G["cast_pending"] = False
            G["blocked"] = (f"cast of {G['cast_pending_name']} never confirmed")
            G["done"] = True
            return
        else:
            return
    # mastery resolved? (exiled as its own last step)
    m_ex = mastery_obj(state, 0, zone="Exile")
    if m_ex is not None and not G.get("mastery_resolved_noted"):
        G["mastery_resolved_noted"] = True
        say(f"[{tag}] Mastery resolved (in exile); maycast_selected="
            f"{G.get('maycast_selected')}")
        wire("mastery_resolved", {"game": G["game_idx"],
                                  "maycast_selected": G.get("maycast_selected")})
    # may-cast select prompt (exile-zone candidates only)?
    mopp = find_maycast_select_opp(st)
    if mopp is not None:
        await answer_maycast_select(c, mopp, state, tag, G)
        return
    # watchdog: mastery resolved but may-cast select never arrived
    selected = G.get("maycast_selected")
    if G.get("mastery_resolved_noted") and selected is None:
        if G.get("resolved_since") is None:
            G["resolved_since"] = time.time()
            say(f"[{tag}] mastery resolved at {G['resolved_since']:.0f}; "
                f"waiting for may-cast select")
        elif time.time() - G["resolved_since"] > 120:
            say(f"[{tag}] no may-cast select 120s after resolution; "
                f"exporting diagnostic")
            wire("no_maycast_prompts",
                 {"game": G["game_idx"],
                  "vi": st.get("viewer_interaction"),
                  "stack": stack_summary(state)})
            try:
                diag = await export_now(f"diag_no_prompts_g{G['game_idx']}.json", c)
                G["diag_no_prompts"] = True
            except Exception as e:
                say(f"[{tag}] diag export failed: {e!r}")
            G["blocked"] = ("mastery resolved but no may-cast select "
                            "appeared")
            G["done"] = True
            return
    # watch for the copies hitting the stack (determines actual order)
    if selected is not None:
        watch_copy_casts(state, G, tag)
    sig = stack_summary(state)
    if sig != G.get("last_stack_sig"):
        G["last_stack_sig"] = sig
        wire("stack", {"game": G["game_idx"], "stack": sig,
                       "stage": "mastery_flow"})
        for nm, ktype, desc in sig:
            if ktype == "TriggeredAbility" and "storm" in desc.lower():
                if not G.get("storm_trigger_seen"):
                    G["storm_trigger_seen"] = True
                    say(f"[{tag}] STORM trigger observed on stack: {desc[:80]}")
                    wire("storm_trigger_seen", {"game": G["game_idx"],
                                                "desc": desc,
                                                "stage": "mastery_flow"})
    # diagnostic: log pending decisions periodically while waiting
    if selected is not None and decision_pending(st):
        if time.time() - G.get("last_dec_diag", 0) > 20:
            G["last_dec_diag"] = time.time()
            vi = st.get("viewer_interaction") or {}
            ops = []
            for o in vi_ops(st):
                r = o.get("response", {}) or {}
                data = r.get("data", {}) or {}
                chs = data.get("choices") or []
                chtxt = []
                for ch in chs[:12]:
                    t = choice_text(ch)
                    codes = surf_codes(ch)
                    chtxt.append(f"{t[:40]}|{codes}")
                ops.append({"rtype": r.get("type"),
                            "n_choices": len(chs),
                            "choices": chtxt,
                            "iid": o.get("interactionId")})
            say(f"[{tag}] waiting: decision pending, vi_kind={vi_kind_code(st)}, "
                f"ops={json.dumps(ops)[:600]}, stack={stack_summary(state)}")
            wire("waiting_decision", {"game": G["game_idx"],
                                      "vi_kind": vi_kind_code(st), "ops": ops,
                                      "stack": stack_summary(state)})
    # watchdog: may-cast answered but the engine never put both copies on
    # the stack (covers an engine-side no-prompt cast failure).
    if (selected is not None and G.get("mastery_resolved_noted")
            and not (G.get("warrens_seen_at") and G.get("ritual_seen_at"))):
        if G.get("copies_wait_since") is None:
            G["copies_wait_since"] = time.time()
            say(f"[{tag}] may-cast answered; waiting for copies on stack")
        elif time.time() - G["copies_wait_since"] > 90:
            say(f"[{tag}] copies never reached the stack 90s after may-cast; "
                "exporting diagnostic")
            wire("copies_never_on_stack",
                 {"game": G["game_idx"], "vi": st.get("viewer_interaction"),
                  "stack": stack_summary(state)})
            try:
                diag = await export_now(
                    f"diag_no_copies_g{G['game_idx']}.json", c)
                G["diag_no_copies"] = True
            except Exception as e:
                say(f"[{tag}] diag export failed: {e!r}")
            G["blocked"] = ("may-cast answered but copies never appeared on "
                            "the stack (engine no-prompt cast path)")
            G["done"] = True
            return
    else:
        G["copies_wait_since"] = None
    # completion: may-cast selected + mastery exiled + both copies seen on the
    # stack at some point + stack now empty + no pending prompt. The settle
    # stage then watches resolution to the final Goblin count.
    if (G.get("mastery_resolved_noted") and selected is not None
            and G.get("warrens_seen_at") and G.get("ritual_seen_at")
            and not stack_entries(state) and not decision_pending(st)):
        if G.get("copies_done_at") is None:
            G["copies_done_at"] = time.time()
        if time.time() - G["copies_done_at"] > 4:
            post = await export_now(f"post_copies_g{G['game_idx']}.json", c)
            G["post_copies"] = post
            G["stack_at_copies"] = stack_summary(post)
            watch_copy_casts(post, G, tag)
            say(f"[{tag}] copies selected; actual_order={G.get('actual_order')}; "
                f"stack={G['stack_at_copies']}")
            wire("copies_complete", {"game": G["game_idx"],
                                     "stack": G["stack_at_copies"],
                                     "maycast_selected": G.get("maycast_selected"),
                                     "actual_order": G.get("actual_order")})
            G["stage"] = "settle"
            G["settle_since"] = time.time()
            return
    else:
        G["copies_done_at"] = None
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def settle_tick(c, G, tag, st, state, acts):
    # watch for copy casts (actual order) + the Storm trigger
    watch_copy_casts(state, G, tag)
    # watch the stack: detect the Storm trigger + its resolution
    sig = stack_summary(state)
    if sig != G.get("last_stack_sig"):
        G["last_stack_sig"] = sig
        wire("stack", {"game": G["game_idx"], "stack": sig})
        for nm, ktype, desc in sig:
            if ktype == "TriggeredAbility" and "storm" in desc.lower():
                if not G.get("storm_trigger_seen"):
                    G["storm_trigger_seen"] = True
                    say(f"[{tag}] STORM trigger observed on stack: {desc[:80]}")
                    wire("storm_trigger_seen", {"game": G["game_idx"],
                                                "desc": desc})
        # opportunistic post-storm export: trigger was seen, now gone, and
        # Warrens copies sit on the stack
        if (G.get("storm_trigger_seen") and not G.get("post_storm")
                and not any(k == "TriggeredAbility" and "storm" in d.lower()
                            for _, k, d in sig)
                and any(n == WARRENS for n, k, _ in sig)):
            try:
                ps = await export_now(f"post_storm_g{G['game_idx']}.json", c)
                G["post_storm"] = ps
                n_w = sum(1 for n, k, _ in stack_summary(ps) if n == WARRENS)
                G["storm_copies_stack_observed"] = n_w
                say(f"[{tag}] post-storm checkpoint: {n_w} Warrens on stack")
            except Exception as e:
                say(f"[{tag}] post-storm export failed: {e!r}")
    # completion: stack empty, no decisions, may-cast done
    if (not stack_entries(state) and not decision_pending(st)
            and G.get("maycast_selected") is not None
            and G.get("mastery_resolved_noted")):
        if G.get("empty_since") is None:
            G["empty_since"] = time.time()
        if time.time() - G["empty_since"] > 6:
            post = await export_now(f"post_g{G['game_idx']}.json", c)
            G["post"] = post
            gobs = goblin_tokens(post, 0)
            G["goblins"] = len(gobs)
            G["goblin_detail"] = [(oid, {"name": o.get("card_name"),
                                          "pt": (o.get("power"), o.get("toughness"))})
                                    for oid, o in gobs]
            say(f"[{tag}] FINAL: {G['goblins']} Goblin tokens; "
                f"selected={G.get('maycast_selected')} "
                f"actual_order={G.get('actual_order')}")
            wire("final", {"game": G["game_idx"], "goblins": G["goblins"],
                           "maycast_selected": G.get("maycast_selected"),
                           "actual_order": G.get("actual_order")})
            G["stage"] = "done"
            G["done"] = True
            return
    else:
        G["empty_since"] = None
    if time.time() - G.get("settle_since", time.time()) > 240:
        say(f"[{tag}] settle timeout")
        G["blocked"] = "settle timed out waiting for empty stack"
        G["done"] = True
        return
    if decision_pending(st):
        # unexpected decision during settle: log it, don't answer blindly
        if not G.get("settle_decision_warned"):
            G["settle_decision_warned"] = True
            say(f"[{tag}] unexpected decision pending during settle")
            wire("settle_unexpected_decision",
                 {"game": G["game_idx"],
                  "vi": st.get("viewer_interaction")})
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    # track P0 turns for the "no spells cast this turn" probe condition
    state = st["state"]
    if state.get("active_player") == 0 and G.get("last_active") != 0:
        G["casts_this_turn"] = 0
    G["last_active"] = state.get("active_player")
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 0, tag, G):
        return
    if await do_bottom(c, acts, st, 0, tag, G):
        return
    if await do_discard(c, st, 0, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # cast-confirmation guard (develop stage casts: Looting)
    if G.get("cast_pending") and G.get("stage") == "develop":
        needs = ST["mana_needs"].get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_tick(c, acts):
                return
            if await pay_mana_vi(c, st, tag, needs):
                return
        if cast_confirmed(state, G):
            say(f"[{tag}] cast {G['cast_pending_name']} confirmed; resuming")
            wire("cast_confirmed", {"name": G["cast_pending_name"],
                                   "game": G["game_idx"]})
            G["cast_pending"] = False
            ST["mana_needs"][tag] = {}
            if G["cast_pending_name"] == LOOTING:
                pass  # looting_resolving cleared by do_discard
        elif time.time() - G.get("cast_pending_at", 0) > 45:
            say(f"[{tag}] cast backstop: {G['cast_pending_name']} unconfirmed")
            G["cast_pending"] = False
            if G["cast_pending_name"] == LOOTING:
                G["looting_resolving"] = False
        else:
            return

    stage = G.get("stage")
    if stage == "develop":
        await develop_tick(c, G, tag, st, state, acts)
    elif stage == "mastery_flow":
        await mastery_flow_tick(c, G, tag, st, state, acts)
    elif stage == "settle":
        await settle_tick(c, G, tag, st, state, acts)
    elif stage == "done":
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
    else:
        if decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision


async def export_now(path, client):
    raw = await client.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(raw)
    say(f"exported {path} ({len(raw)} bytes)")
    env = json.loads(raw)
    st = env.get("state", env) if isinstance(env, dict) else env
    return st


def dump_view(c, st):
    s = st["state"]
    acts = [a.get("type") for a in (st.get("legal_actions") or [])]
    vi = st.get("viewer_interaction") or {}
    wire("view", {"who": c.name, "rev": c.revision,
                  "turn": turn_of(s), "phase": s.get("phase"),
                  "active": s.get("active_player"), "top_acts": acts,
                  "vi_can": vi.get("canSubmit"),
                  "vi_kind": (vi.get("waitingForKind") or {}).get("code"),
                  "stack": stack_summary(s)})


def reset_per_game():
    global PASSED_REV, SUBMITTED_OPPS, LAND_PLAYED_THIS_ACTIVE
    PASSED_REV = {}
    SUBMITTED_OPPS = set()
    LAND_PLAYED_THIS_ACTIVE = {}
    ST["mana_needs"] = {}


# ------------------------------------------------------------- game runner
async def run_one_game(order_pref, game_idx):
    reset_per_game()
    G = {"game_idx": game_idx, "order_pref": order_pref,
         "stage": "develop",
         "looting_resolving": False,
         "cast_pending": False, "cast_pending_name": None,
         "cast_pending_oid": None, "cast_pending_at": 0.0,
         "casts_this_turn": 0, "last_active": None,
         "maycast_selected": None, "actual_order": None,
         "warrens_seen_at": None, "ritual_seen_at": None,
         "mastery_on_stack": False, "mastery_resolved_noted": False,
         "copies_done_at": None, "settle_since": 0, "empty_since": None,
         "last_stack_sig": None, "storm_trigger_seen": False,
         "storm_copies_stack_observed": None,
         "pre_mastery": None, "post_copies": None, "post_storm": None,
         "post": None, "stack_at_copies": None,
         "goblins": None, "goblin_detail": None,
         "mastery_cast_turn": None, "mastery_cast_data": None,
         "max_turn": 0, "mulls0": 0, "mulls1": 0,
         "rejections": [], "settle_decision_warned": False,
         "done": False, "blocked": None, "game_ok": False}

    p0 = PhaseClient(f"9721-P0-g{game_idx}")
    p1 = PhaseClient(f"9721-P1-g{game_idx}")
    await p0.connect()
    await p1.connect()
    att = await p0.create(P0_DECK, player_count=2)
    code = att.get("game_code")
    G["game_code"] = code
    say(f"[game{game_idx}] created code={code} order_pref={order_pref}")
    wire("game_created", {"game_code": code, "game_idx": game_idx,
                          "order_pref": order_pref})
    await p1.join(code, P1_DECK)

    timeout_s = 1500
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    ticks = [(p0, p0_tick, 0, "P0"), (p1, p1_tick, 1, "P1")]
    try:
        for _ in range(int(timeout_s / 0.2)):
            await asyncio.sleep(0.2)
            for c, tick, pid, tag in ticks:
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                if (rev == last.get(c.name)
                        and time.time() - last_tick.get(c.name, 0) <= 5):
                    continue
                last[c.name] = rev
                last_tick[c.name] = time.time()
                last_adv[c.name] = time.time()
                warned.discard(c.name)
                state = st["state"]
                dump_view(c, st)
                G["max_turn"] = max(G["max_turn"], turn_of(state))
                try:
                    await tick(c, G, tag)
                except Exception as e:
                    say(f"[{c.name}] tick error: {e!r}")
                    wire("tick_error", {"who": c.name, "error": repr(e)})
                if G.get("done"):
                    say(f"[game{game_idx}] contract complete; finishing")
                    G["game_ok"] = True
                    return G
            now = time.time()
            if now - last_diag > 90 and p0.latest:
                last_diag = now
                s = p0.latest["state"]
                say(f"[game{game_idx}] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                    f"phase={s.get('phase')} P0hand={len(hand_names(s,0))} "
                    f"stack={len(stack_entries(s))} stage={G.get('stage')} "
                    f"gy_w={gy_count(s,0,WARRENS)} gy_r={gy_count(s,0,RITUAL)} "
                    f"casts_tt={G.get('casts_this_turn')} "
                    f"sel={G.get('maycast_selected')} "
                    f"order={G.get('actual_order')}")
            for c in (p0, p1):
                if (now - last_adv[c.name] > 90 and c.name not in warned
                        and c.latest):
                    warned.add(c.name)
                    st = c.latest
                    state = st.get("state", {})
                    say(f"[game{game_idx}] WATCHDOG {c.name}: no revision for "
                        f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                        f"turn={turn_of(state)} phase={state.get('phase')} "
                        f"vi_kind={vi_kind_code(st)} stage={G.get('stage')}")
            if now - t0 > timeout_s:
                say(f"[game{game_idx}] global timeout ({timeout_s}s)")
                G["blocked"] = "global timeout before completion"
                return G
    finally:
        await p0.close()
        await p1.close()
    G["blocked"] = G.get("blocked") or "loop exhausted without completion"
    return G


# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"))
    out = {}
    for key in ("mizzix's mastery", "empty the warrens", "dark ritual"):
        c = cd[key]
        out[key] = {"oracle_text": c.get("oracle_text"),
                    "mana_cost": c.get("mana_cost"),
                    "abilities": c.get("abilities")}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    return out


# ------------------------------------------------------------- evaluation
def evaluate_game(G, game_idx):
    ass = {}
    notes = []
    pre = G.get("pre_mastery") or {}
    if G.get("blocked") and G.get("mastery_cast_turn") is None:
        for k in ("A1_setup_ok", "A2_mastery_overloaded", "A3_copies_cast",
                  "A4_order", "A5_storm_copies", "A6_goblins"):
            ass[k] = "not-run"
        notes.append(f"game{game_idx}: blocked before probe - {G['blocked']}")
        return ass, notes, "blocked"

    # A1: probe point
    if G.get("mastery_cast_turn") is not None and pre:
        gyw = gy_count(pre, 0, WARRENS)
        gyr = gy_count(pre, 0, RITUAL)
        lands = len([o for _, o in bf(pre, 0) if is_land_obj(o)])
        mtn = n_mountains_bf(pre, 0)
        ok = (gyw >= 1 and gyr >= 1 and lands >= 8 and mtn >= 3
              and find_hand(pre, 0, MASTERY) is not None)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"game{game_idx} A1 {'passed' if ok else 'FAILED'}: "
                     f"pre-mastery gy warrens={gyw} ritual={gyr}, "
                     f"lands={lands} mountains={mtn}, mastery in hand="
                     f"{find_hand(pre, 0, MASTERY) is not None}, "
                     f"cast turn={G['mastery_cast_turn']}, prior casts=0")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"game{game_idx} A1 FAILED: probe point never reached "
                     f"(max_turn={G['max_turn']}, stage={G.get('stage')})")

    # A2: overloaded mastery cast + resolved. Overload is proven by BOTH
    # cards being exiled (normal mode exiles a single target).
    mdata = G.get("mastery_cast_data") or {}
    blob = json.dumps(mdata).lower()
    post_copies = G.get("post_copies") or {}
    ex = exile_names(post_copies, 0) if post_copies else []
    both_exiled = (WARRENS in ex and RITUAL in ex)
    mode_chosen = G.get("overload_mode_chosen", False)
    overloaded = both_exiled or "overload" in blob or mode_chosen
    if G.get("mastery_resolved_noted"):
        ass["A2_mastery_overloaded"] = "passed" if overloaded else "failed"
        notes.append(f"game{game_idx} A2 {'passed' if overloaded else 'FAILED'}: "
                     f"Mastery resolved (exiled); both cards exiled={both_exiled} "
                     f"(exile={ex}); overload marker in cast data="
                     f"{'overload' in blob}; mode prompt chose overload={mode_chosen}")
    else:
        ass["A2_mastery_overloaded"] = "failed" if G.get("mastery_cast_turn") else "not-run"
        notes.append(f"game{game_idx} A2: mastery "
                     f"{'cast but not confirmed resolved' if G.get('mastery_cast_turn') else 'never cast'}")

    # A3: both copies selected for casting (Warrens + Ritual, no Lootings)
    selected = G.get("maycast_selected")
    if selected is not None and set(selected) == {WARRENS, RITUAL}:
        ass["A3_copies_cast"] = "passed"
        notes.append(f"game{game_idx} A3 passed: may-cast selected exactly "
                     f"Warrens+Ritual (selection order={selected}; "
                     f"Lootings excluded)")
    elif selected is not None:
        ass["A3_copies_cast"] = "failed"
        notes.append(f"game{game_idx} A3 FAILED: may-cast selection={selected}")
    else:
        ass["A3_copies_cast"] = "failed" if G.get("mastery_resolved_noted") else "not-run"
        notes.append(f"game{game_idx} A3: may-cast never selected "
                     f"(resolved={G.get('mastery_resolved_noted')})")

    # A4: order (stack-observed actual order, fallback to selection order)
    want_first = WARRENS if G.get("order_pref") == "warrens_first" else RITUAL
    actual_first = G.get("actual_order")
    if actual_first is None and selected is not None:
        actual_first = selected[0]
        notes.append(f"game{game_idx} A4 note: stack order not observed; "
                     f"using selection order as proxy")
    if actual_first in (WARRENS, RITUAL):
        ok = (actual_first == want_first)
        ass["A4_order"] = "passed" if ok else "failed"
        notes.append(f"game{game_idx} A4 {'passed' if ok else 'FAILED'}: "
                     f"actual first={actual_first}, wanted={want_first} "
                     f"(stack-observed={G.get('actual_order')})")
    else:
        ass["A4_order"] = "not-run"
        notes.append(f"game{game_idx} A4 not-run: order not determined")

    # A5/A6: storm copies + goblins
    gobs = G.get("goblins")
    if gobs is not None:
        storm_copies = gobs // 2 - 1
        exp_gobs = 4 if G.get("order_pref") == "warrens_first" else 6
        exp_storm = 1 if G.get("order_pref") == "warrens_first" else 6 // 2 - 1
        ok6 = (gobs == exp_gobs)
        ass["A6_goblins"] = "passed" if ok6 else "failed"
        notes.append(f"game{game_idx} A6 {'passed' if ok6 else 'FAILED'}: "
                     f"{gobs} Goblin tokens (expected {exp_gobs} for "
                     f"{G.get('order_pref')})")
        ok5 = (storm_copies == exp_storm)
        ass["A5_storm_copies"] = "passed" if ok5 else "failed"
        notes.append(f"game{game_idx} A5 {'passed' if ok5 else 'FAILED'}: "
                     f"storm copies={storm_copies} (expected {exp_storm}); "
                     f"stack-observed={G.get('storm_copies_stack_observed')}; "
                     f"storm trigger seen={G.get('storm_trigger_seen')}")
    else:
        ass["A5_storm_copies"] = "not-run"
        ass["A6_goblins"] = "not-run"
        notes.append(f"game{game_idx} A5/A6 not-run: no final goblin count")

    # per-game verdict contribution (keys off the ACTUAL cast order)
    first = G.get("actual_order")
    if first is None and selected is not None:
        first = selected[0]
    if first == WARRENS and gobs is not None:
        if gobs == 6:
            notes.append(f"game{game_idx}: BUG SIGNATURE - Warrens->Ritual "
                         f"order produced 6 Goblins (2 Storm copies); the "
                         f"later Ritual cast was counted")
            return ass, notes, "reproduced"
        elif gobs == 4:
            notes.append(f"game{game_idx}: Warrens->Ritual order produced 4 "
                         f"Goblins (1 Storm copy) - correct CR 702.40a behavior")
            return ass, notes, "not-reproduced"
        else:
            notes.append(f"game{game_idx}: unexpected goblin count {gobs}")
            return ass, notes, "blocked"
    if first == RITUAL and gobs is not None:
        if gobs == 6:
            notes.append(f"game{game_idx}: control consistent - Ritual->Warrens "
                         f"legitimately produced 6 Goblins (2 Storm copies)")
        else:
            notes.append(f"game{game_idx}: control ANOMALY - Ritual->Warrens "
                         f"produced {gobs} Goblins (expected 6)")
        return ass, notes, "control"
    notes.append(f"game{game_idx}: inconclusive (order/casts/goblins incomplete)")
    return ass, notes, "blocked"


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1180
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#9721 Storm counts later no-prompt copy casts (Mizzix's Mastery)",
         fill=(255, 210, 90))
    s = run["server"]
    line(f"server {s['server_version']} build {s['build_commit']} protocol "
         f"{s['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 Mastery + Warrens + Ritual binned, 8 lands (3R); overload "
         "Mastery, cast both copies, resolve Storm", size=16)
    line("expected: W->R order => 1 Storm copy => 4 Goblins; "
         "R->W order => 2 copies => 6 Goblins (control)", size=16)
    y += 6
    for gi, gr in enumerate(run["games"]):
        line(f"game {gi+1} ({gr['order_pref']}):", fill=(160, 200, 255))
        line(f"  selected={gr.get('maycast_selected')} order={gr.get('actual_order')} goblins={gr.get('goblins')} "
             f"storm_copies={gr.get('storm_copies')} -> {gr['verdict']}", size=17)
        for k, val in gr["assertions"].items():
            col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
                  if val == "failed" else (200, 200, 200))
            line(f"    {k}: {val}", fill=col, size=15)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:12]:
        line(f"  - {n[:110]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.106.0, protocol 126)")
    await verify_server_hello()
    parse = parse_evidence()
    say("parse: mastery/warrens/ritual oracle captured")

    games = []
    try:
        g1 = await run_one_game("warrens_first", 1)
    except Exception as e:
        say(f"game1 crashed: {e!r}")
        wire("game_crashed", {"game": 1, "error": repr(e)})
        g1 = {"game_idx": 1, "order_pref": "warrens_first",
              "blocked": f"game crashed: {e!r}", "maycast_selected": None,
              "goblins": None, "rejections": [], "max_turn": 0,
              "mastery_cast_turn": None}
    a1, n1, v1 = evaluate_game(g1, 1)
    games.append({"order_pref": "warrens_first", "assertions": a1,
                  "notes": n1, "verdict": v1,
                  "maycast_selected": g1.get("maycast_selected"),
                  "actual_order": g1.get("actual_order"),
                  "goblins": g1.get("goblins"),
                  "storm_copies": (g1["goblins"] // 2 - 1)
                  if g1.get("goblins") is not None else None,
                  "game_code": g1.get("game_code"),
                  "mastery_cast_turn": g1.get("mastery_cast_turn")})

    # game 2 (control) only if game 1 exercised the path or we need the data
    run_g2 = True
    if v1 == "blocked" and not g1.get("mastery_cast_turn"):
        say("game1 blocked before probe; skipping game 2 (same driver path)")
        run_g2 = False
    if run_g2:
        try:
            g2 = await run_one_game("ritual_first", 2)
        except Exception as e:
            say(f"game2 crashed: {e!r}")
            wire("game_crashed", {"game": 2, "error": repr(e)})
            g2 = {"game_idx": 2, "order_pref": "ritual_first",
                  "blocked": f"game crashed: {e!r}", "maycast_selected": None,
                  "goblins": None, "rejections": [], "max_turn": 0,
                  "mastery_cast_turn": None}
        a2, n2, v2 = evaluate_game(g2, 2)
        games.append({"order_pref": "ritual_first", "assertions": a2,
                      "notes": n2, "verdict": v2,
                      "maycast_selected": g2.get("maycast_selected"),
                  "actual_order": g2.get("actual_order"),
                      "goblins": g2.get("goblins"),
                      "storm_copies": (g2["goblins"] // 2 - 1)
                      if g2.get("goblins") is not None else None,
                      "game_code": g2.get("game_code"),
                      "mastery_cast_turn": g2.get("mastery_cast_turn")})
    else:
        g2 = None

    notes = ["2026-10-11 04:11 port of scenario_9721.py: P0 mulligans until "
             "Mizzix's Mastery is in the opening hand (cap 6; bottoming keeps "
             "Mastery), develop turn cap 100 -> blocked. Interaction "
             "conventions unchanged from the 02:41 driver."]
    for gr in games:
        notes.extend(gr["notes"])
    verdicts = [gr["verdict"] for gr in games]
    if "reproduced" in verdicts:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - a Warrens->Ritual game produced "
                     "the predicted 6-Goblin / 2-Storm-copy miscount")
    elif "not-reproduced" in verdicts:
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - the Warrens->Ritual path was "
                     "exercised and Storm counted correctly (4 Goblins)")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - the reported path could not be "
                     "exercised to a verdict")

    dur = time.time() - t_start

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    bin_path = f"{BACKFILL}/server/releases/v0.106.0/phase-server-slim-x86_64-unknown-linux-musl"
    cd_path = f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.106.0/data/draft-pools.json"
    server_identity = {
        "server_version": "0.106.0",
        "build_commit": "29e0db3",
        "protocol_version": 126,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY (436711b6a2d36828)",
        "signature_verified": True,
        "signature_note": ("v0.106.0 binary + release manifest minisign-verified "
                           "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                           "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                           "mode; data digests match the signed manifest; digests "
                           "recomputed against on-disk files this run"),
        "source": ("2026-10-11: latest stable release v0.106.0 (published "
                   "2026-10-10T17:09:24Z) == pinned release dir (pinned "
                   "2026-10-09); ServerHello 0.106.0/29e0db3/protocol 126 "
                   "verified by handshake this run; hashes recomputed against "
                   "on-disk artifacts this run; backfill-owned v0.106.0 server "
                   "on 127.0.0.1:9374 (already running; reused per playbook)"),
    }
    assert server_identity["binary_sha256"] == \
        "5d31aeec7387c22b5af84f7befa83c14f785d8505c0f9102f4fddac743d1d47a", \
        "binary digest drift vs pinned v0.106.0 record"
    assert server_identity["card_data_sha256"] == \
        "59d60da9833ff7cd464fca2c93f4d6b49d74a8ad0d04092055634442c70cbc41", \
        "card-data digest drift vs pinned v0.106.0 record"
    assert server_identity["draft_pools_sha256"] == \
        "fad736f3ae13b3fb2f0908319eae98af501e36cc30fbe2e84ca6684319384c86", \
        "draft-pools digest drift vs pinned v0.106.0 record"

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": "runs/run-20261011-0241",  # 04:11 run reuses the live backfill-owned v0.106.0 server
        "driver": {"protocol_advertised": 126, "client": "driver/client.py"},
        "scenario": "driver/scenario_9721_01060.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_9721_01060.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "games": games,
        "assertions": {f"g{i+1}_{k}": v
                       for i, gr in enumerate(games)
                       for k, v in gr["assertions"].items()},
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-10-11",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P1 is a passive second seat (lands only, no attacks, no spells).",
            "Storm copy count is measured as goblins/2 - 1 (each Warrens "
            "resolution creates exactly two 1/1 Goblins; no other Goblin "
            "source in either deck); stack-level observation is best-effort.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "history": [],
        "setup_line": ("P0 4x Mizzix's Mastery + 4x Empty the Warrens + 4x Dark "
                       "Ritual + 8x Faithless Looting + 20x Mountain + 24x "
                       "Island vs P1 60x Island. P0 bins Warrens+Ritual "
                       "(Looting/cleanup discard), develops to 8 lands (3+ "
                       "Mountains), casts overloaded Mastery on a turn with "
                       "no prior casts, accepts both may-cast copies, lets "
                       "the Storm trigger and copies resolve."),
        "contract_line": ("Warrens->Ritual copy order must yield 1 Storm copy "
                          "/ 4 Goblins (CR 702.40a: only Mastery was cast "
                          "before Warrens); 6 Goblins / 2 Storm copies is "
                          "the reported miscount signature. Ritual->Warrens "
                          "legitimately yields 6 Goblins (control)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    subprocess.run(["cp", __file__, f"{EVDIR}/scenario_9721_01060.py"], check=True)
    render_png(f"{EVDIR}/summary.png", run)

    try:
        slog = f"{BACKFILL}/runs/run-20261011-0241/server.log"
        excerpts = []
        codes = [g.get("game_code") for g in (g1, g2) if g and g.get("game_code")]
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    ll = line.lower()
                    if any(c and c in line for c in codes):
                        excerpts.append(line.rstrip())
                    elif any(k in ll for k in ("storm", "cast_copy", "mizzix",
                                              "warrens", "error", "warn")):
                        excerpts.append(line.rstrip()[:300])
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest.sha256 written for {len(lines)} files", flush=True)
    bad = []
    for line in lines:
        h, _, name = line.partition("  ")
        h2 = sha(f"{EVDIR}/{name.strip()}")
        if h2 != h:
            bad.append(name)
    assert not bad, f"manifest self-check failed: {bad}"
    print(f"manifest self-check passed for {len(lines)} files", flush=True)
    for fn in os.listdir(EVDIR):
        if fn.endswith(".json"):
            json.load(open(f"{EVDIR}/{fn}"))
    im = __import__("PIL.Image", fromlist=["Image"]).open(
        f"{EVDIR}/summary.png")
    im.verify()
    print("evidence validated: JSON parses, manifest self-checks, PNG readable",
          flush=True)
    print(json.dumps({"verdict": verdict,
                      "games": [{k: gr[k] for k in ("order_pref", "verdict",
                                                    "maycast_selected", "actual_order", "goblins")}
                                for gr in games]}, indent=1), flush=True)


asyncio.run(amain())
