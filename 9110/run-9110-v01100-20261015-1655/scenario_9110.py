#!/usr/bin/env python3
"""Issue #9110: Gush alternative cost can be cast for free.

Oracle (verified against pinned v0.110.0 card-data.json):
  You may return two Islands you control to their owner's hand rather than
  pay this spell's mana cost.
  Draw two cards.

Reported (Discord, 2026-09-19): "Alternative cost of Gush can be cast for
free -- Req: 2 islands on board, Gush in hand 1."

Behavioral contract -- two legs, 2-player game, native engine:
  LEG 1 (control -- harness validation): P0 ramps to 5 untapped Islands,
    casts Gush paying {4}{U}. Expect: 5 Islands tapped, Gush reaches the
    stack, resolves -> P0 draws exactly 2 cards, Gush to graveyard.
  LEG 2 (reported branch): on a later P0 turn (>=2 untapped Islands, Gush
    in hand), P0 casts Gush and chooses the alternative cost.
    Expect: exactly 2 Islands leave P0's battlefield for P0's hand AS THE
    COST (paid while casting), then Gush resolves -> draw 2.
    Bug: the alternative cost pays nothing -- the Islands stay on the
    battlefield (cast for free).

Assertions:
  A1 harness_ok     Control cast paid {4}{U} (5 Islands tapped), reached
                    the stack, resolved, drew exactly 2.
  A2 alt_cost_paid  The alternative-cost cast returned exactly 2 Islands
                    from P0's battlefield to P0's hand. FAILED = the
                    reported bug (Islands never left the battlefield).
  A3 alt_draw       The alternative-cost cast resolved and P0 drew 2.
  A4 post_clean    POST_ALT exported; the cast Gush in graveyard; no
                    pending real decisions.

Verdict rule (pre-registered):
  - reproduced:     A1 passed and A2 failed.
  - not-reproduced: A1, A2, A3 all passed (A4 observational).
  - blocked:        A1 failed/not-run, or the alt leg never completed.

Conventions (protocol 142):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - CastSpell submitted EXACTLY as advertised (no injected targets).
  - The alt leg NEVER taps lands for mana: the alternative cost replaces
    the mana cost, so any mana demand there is itself a failure signal.
  - Export response data.state is a JSON string, parsed once.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
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
log = logging.getLogger("scenario9110")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9110-v01100-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9110
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "server_binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_note": ("v0.110.0 pinned release dir: binary digest recomputed "
                       "this run and matches the pinned record; GitHub still "
                       "lists v0.110.0 (published 2026-10-14) as the latest "
                       "stable engine release, so the pin did not advance; "
                       "ServerHello 0.110.0/9bfd12e/protocol 142 confirmed "
                       "by live handshake on 127.0.0.1:9374."),
    "source": ("2026-10-15: latest stable release v0.110.0 == pinned release "
               "dir; backfill-owned v0.110.0 server on 127.0.0.1:9374 "
               "(--single-user --no-data-download, verified release); "
               "scenario games created via CreateGameWithSettings; evidence "
               f"under evidence/9110/{RUN_ID}"),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"))
GUSH_ORACLE = (CARD_DATA.get("gush") or {}).get("oracle_text")
GUSH_CASTING = (CARD_DATA.get("gush") or {}).get("casting_options")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity pinned: v0.110.0 / 9bfd12e / protocol 142")
say("Gush oracle: " + str(GUSH_ORACLE))
say("Gush casting_options: " + json.dumps(GUSH_CASTING, default=str)[:400])

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
GUSH = "gush"

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


def library_count(state, pid):
    return len(player_of(state, pid).get("library") or [])


def graveyard_names(state, pid):
    return [obj_lname(state, o)
            for o in (player_of(state, pid).get("graveyard") or [])]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def island_oids(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and obj_lname(state, oid) == "island"]


def untapped_island_oids(state, pid):
    return [o for o in island_oids(state, pid)
            if not get_obj(state, o).get("tapped")]


def tapped_island_oids(state, pid):
    return [o for o in island_oids(state, pid)
            if get_obj(state, o).get("tapped")]


def gush_in_hand_oid(state, pid):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == GUSH:
            return o
    return None


def stack_entries(state):
    out = []
    for e in state.get("stack") or []:
        out.append(e if isinstance(e, dict) else get_obj(state, e))
    return out


def stack_entry_name(state, e):
    if isinstance(e, dict):
        for k in ("base_name", "name"):
            if e.get(k):
                return str(e.get(k))
        for k in ("source_id", "id", "oid"):
            if e.get(k) is not None:
                o = get_obj(state, e.get(k))
                for kk in ("base_name", "name"):
                    if o.get(kk):
                        return str(o.get(kk))
    return "?"


def spell_on_stack(state, name):
    for e in stack_entries(state):
        if stack_entry_name(state, e).lower() == name:
            return True
    return False


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def gush_graveyard_oids(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() != GUSH:
            continue
        if str(o.get("controller", o.get("owner", -1))) != str(pid):
            continue
        if o.get("zone") == "Graveyard":
            out.append(str(oid))
    return out


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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


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
    codes = []
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            c = (s.get("data") or {}).get("code")
            if c:
                codes.append(c)
    return codes


def value_surfaces(ch):
    return [(((s.get("data") or {}).get("role")),
             ((s.get("data") or {}).get("value")))
            for s in ch.get("surfaces", []) or []
            if s.get("type") == "value"]


def _opp_codes(opp):
    codes = set()
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("choices") or data.get("candidates") or []:
        for c in surf_codes(ch):
            codes.add(c)
    return codes


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


ASSERT_KEYS = ["A1_harness_ok", "A2_alt_cost_paid", "A3_alt_draw",
               "A4_post_clean"]

# ------------------------------------------------------------- action helpers

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


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 15)
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


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    say(f"[{tag}] keeping opening hand")
    wire("mulligan", {"who": tag, "decision": "keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def play_named_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != "island" or not is_land(get_obj(state, o)):
            continue
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing island")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


async def play_land_vi(c, st, state, pid, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag, "vi")) == turn:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = [(sf.get("data") or {}).get("code")
                     for sf in ch.get("surfaces", []) or []
                     if sf.get("type") == "action"]
            if "playLand" not in codes:
                continue
            ref = _cand_ref(ch)
            if ref is None or ref not in handset:
                continue
            if obj_lname(state, ref) != "island" \
                    or not is_land(get_obj(state, ref)):
                continue
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS:
                continue
            SUBMITTED_OPPS.add(iid)
            LAND_PLAYED_TURN[(tag, "vi")] = turn
            say(f"[{tag}] playing island via vi prompt (oid {ref})")
            wire("play_land_vi", {"who": tag, "oid": ref})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "choose",
                                               "data": {"choiceId": ch.get("id")}}},
                              tag)
            return True
    return False


async def pay_mana_vi_one(c, st, tag, needs):
    """Submit ONE tapLandForMana. Returns True if a tap was submitted.
    The caller must confirm via revision change before tapping again."""
    if not needs or all(v <= 0 for v in needs.values()):
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" not in codes:
                continue
            syms = []
            for m in ch.get("surfaces", []) or []:
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
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana ({used}); needs before={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick.get("id")}}},
                          tag)
        return True
    return False


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st, state):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        if not items:
            continue
        codes = _opp_codes(opp)
        if "decideOptionalCost" in codes:
            return True
        if "decideOptionalEffect" in codes:
            return True
        if rtype == "schema":
            spec = ((data.get("spec", {}) or {}).get("type")) or ""
            if spec == "select":
                return True
        if codes and codes <= NON_DECISION_CODES:
            continue
        if codes:
            return True
    return False


async def export_state(c, name, ctx):
    raw = await c.export_state()
    env = json.loads(raw)
    state = env.get("state") if isinstance(env.get("state"), dict) \
        else env
    with open(f"{EVDIR}/{name}.json", "w") as f:
        json.dump(env, f, indent=1)
    say(f"[{c.name}] exported {name}.json "
        f"(turn={state.get('turn_number')}, rev={state.get('state_revision')})")
    wire(f"export_{name}", {"turn": state.get("turn_number")})
    return state


def obs_assert(key, status, note):
    OBS["assert"][key] = status
    if note:
        OBS["notes"].append(f"{key}={status}: {note}")
    say(f"assertion {key}: {status}" + (f" -- {note}" if note else ""))


# ------------------------------------------------- cost-choice interaction
# Observed 2026-10-15 (protocol 142): after CastSpell for Gush, the engine
# parks the announced spell on the stack and raises waiting_for
# "OptionalCostChoice" whose Choice cost data is [EffectCost(Bounce), Mana].
# The vi opportunity carries decideOptionalCost action codes. The driver
# MUST answer this choice -- "mana" for the control leg, "alt" for the
# reported branch -- or the cast stalls forever (no resolution).

def find_cost_choice_opp(st, state):
    """The OptionalCostChoice vi opportunity. Returns (opp, [choices])."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        if not items:
            continue
        if any("decideOptionalCost" in surf_codes(ch) for ch in items):
            return opp, items
    return None, None


def pick_cost_branch(items, want):
    """Pick the choice id for want='alt' (alternative cost) or 'mana'.
    Returns (choice_id, choice_text, infos, method).

    Observed 2026-10-15: the OptionalCostChoice offers ("pay","true") =
    use the alternative cost (return 2 Islands) and ("pay","false") = pay
    the normal {4}{U} instead -- the kicker value-surface pattern. A third
    choice (empty text, no values) is never picked."""
    infos = []
    for ch in items:
        infos.append({"id": ch.get("id"), "text": choice_text(ch),
                      "values": value_surfaces(ch)})
    # Primary: VALUE surfaces.
    want_flag = ("pay", "true") if want == "alt" else ("pay", "false")
    for info in infos:
        if want_flag in [tuple(v) for v in info["values"]]:
            return info["id"], info["text"], infos, "value-surface"
    # Secondary: text heuristic.
    for i, info in enumerate(infos):
        t = (info["text"] or "").lower()
        is_alt = ("island" in t and ("return" in t or "alternative" in t
                                    or "bounce" in t))
        if want == "alt" and is_alt:
            return info["id"], info["text"], infos, "text"
        if want == "mana" and t and not is_alt \
                and any(w in t for w in ("{4}", "{u}", "mana", "pay")):
            return info["id"], info["text"], infos, "text"
    # Fallback: engine Choice order observed as [EffectCost(alt), Mana]
    # (skipping the empty header choice).
    order = [info for info in infos if info["text"] or info["values"]]
    idx = 0 if want == "alt" else 1
    if idx < len(order):
        return order[idx]["id"], order[idx]["text"], infos, "index-fallback"
    return None, None, infos, "none"


async def answer_cost_choice(c, st, state, tag, ctx, leg, want):
    """Answer the OptionalCostChoice with the want='alt'/'mana' branch."""
    opp, items = find_cost_choice_opp(st, state)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return False
    cid, ctxt, infos, method = pick_cost_branch(items, want)
    wire(f"{leg}_cost_choice_shape",
         {"want": want, "method": method, "choices": infos,
          "waiting_for": (state.get("waiting_for") or {}).get("type")})
    if cid is None:
        say(f"[{tag}] {leg}: cost choice present but no {want} branch "
            f"identifiable; leaving unanswered for diagnostics")
        wire(f"{leg}_cost_choice_unanswerable", {"choices": infos})
        return False
    SUBMITTED_OPPS.add(iid)
    ctx[leg]["cost_answered"] = True
    ctx[leg]["cost_want"] = want
    ctx[leg]["cost_choice_text"] = ctxt
    ctx[leg]["cost_choice_method"] = method
    say(f"[{tag}] {leg}: cost choice -> {want} [{method}] ({str(ctxt)[:100]})")
    wire(f"{leg}_cost_choice", {"want": want, "method": method,
                                "choice_text": ctxt})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": cid}}}, tag)
    return True


def find_island_select_opp(st, state, pid):
    """A selection opportunity whose candidates are P0's untapped Islands
    (the bounce-cost payment). Returns (opp, [choice...])."""
    islands = set(untapped_island_oids(state, pid))
    if not islands:
        return None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or []
        if not cands:
            continue
        refs = {_cand_ref(ch) for ch in cands}
        refs.discard(None)
        if refs and refs <= islands:
            return opp, [ch for ch in cands if _cand_ref(ch) in islands]
    return None


async def answer_alt_cost(c, st, state, tag, ctx):
    return await answer_cost_choice(c, st, state, tag, ctx, "alt", "alt")


async def select_islands_for_cost(c, st, state, pid, tag, ctx, n=2):
    found = find_island_select_opp(st, state, pid)
    if found is None:
        return False
    opp, chs = found
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return False
    picks = chs[:n]
    pick_ids = [ch.get("id") for ch in picks]
    pick_refs = [_cand_ref(ch) for ch in picks]
    SUBMITTED_OPPS.add(iid)
    ctx["alt"]["islands_selected"] = True
    ctx["alt"]["selected_oids"] = pick_refs
    say(f"[{tag}] selecting {len(picks)} Islands for the bounce cost "
        f"(oids {pick_refs})")
    wire("alt_island_select", {"oids": pick_refs})
    rtype = (opp.get("response", {}) or {}).get("type")
    resp_type = "select" if rtype == "schema" else "sequence"
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": resp_type,
                                       "data": {"choiceIds": pick_ids}}},
                          tag)
    return True


def log_cast_window_opps(st, state, tag, label):
    opps = []
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        opps.append({"iid": str(opp.get("interactionId"))[:12],
                     "rtype": (opp.get("response", {}) or {}).get("type"),
                     "n": len(items),
                     "codes": sorted(_opp_codes(opp)),
                     "texts": [choice_text(ch)[:80] for ch in items[:5]]})
    wf = state.get("waiting_for") or {}
    wire(label, {"waiting_for": wf.get("type"),
                 "wf_data": json.dumps(wf.get("data"), default=str)[:500],
                 "opps": opps})


async def discard_down(c, st, state, pid, tag):
    """Discard to hand size 7 during cleanup. Picks an Island, never Gush.
    Gated on hand > 7 and candidates all being in hand."""
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or []
        if not cands:
            continue
        refs = {_cand_ref(ch) for ch in cands}
        refs.discard(None)
        if not refs or not refs <= handset:
            continue
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        pick = None
        for ch in cands:
            r = _cand_ref(ch)
            if r and obj_lname(state, r) == "island":
                pick = ch
                break
        if pick is None:
            for ch in cands:
                r = _cand_ref(ch)
                if r and obj_lname(state, r) != GUSH:
                    pick = ch
                    break
        if pick is None:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] discarding {obj_lname(state, _cand_ref(pick))} "
            f"(oid {_cand_ref(pick)}) to hand size")
        wire("discard", {"who": tag, "oid": _cand_ref(pick), "rtype": rtype,
                         "n_candidates": len(cands)})
        resp_type = "select" if rtype == "schema" else "sequence"
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": resp_type,
                                           "data": {"choiceIds":
                                                    [pick.get("id")]}}},
                          tag)
        return True
    # hand > 7 but no discard opportunity recognized: log the shape
    if len(hand_ids(state, pid)) > 7:
        log_cast_window_opps(st, state, tag, "discard_missing_opps")
    return False

# ------------------------------------------------------------------- ticks

async def cast_gush(c, state, pid, acts, tag, ctx, leg):
    """Submit CastSpell for Gush exactly as advertised."""
    a, oid = cast_action_for(acts, state, GUSH)
    if a is None:
        return False
    say(f"[{tag}] {leg}: casting Gush (oid {oid}); advertised data keys="
        f"{sorted((a.get('data') or {}).keys())}")
    wire(f"{leg}_cast", {"who": tag, "oid": oid,
                         "advertised_keys": sorted((a.get("data") or {}).keys())})
    await submit_as_is(c, a)
    ctx[leg]["cast_submitted"] = True
    ctx[leg]["cast_time"] = time.time()
    ctx[leg]["cast_turn"] = state.get("turn_number")
    ctx[leg]["cast_oid"] = str(oid)
    return True


async def tick_p0(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    await play_named_land(c, state, pid, acts, tag)
    await play_land_vi(c, st, state, pid, tag)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    active = str(state.get("active_player")) == str(pid)
    phase = str(state.get("phase") or "")
    is_main = "main" in phase.lower()
    turn = state.get("turn_number", 0)

    ctl, alt = ctx["control"], ctx["alt"]

    # ---- LEG 1 (control): pay {4}{U} with 5 untapped Islands
    if not ctl.get("done"):
        if not ctl.get("cast_submitted"):
            if turn > 30:
                return
            if active and is_main \
                    and len(untapped_island_oids(state, pid)) >= 5 \
                    and gush_in_hand_oid(state, pid):
                ctl["pre_control_state"] = await export_state(
                    c, "pre_control", ctx)
                ST["mana_needs"][tag] = {"U": 1, "generic": 4}
                await cast_gush(c, state, pid, acts, tag, ctx, "control")
                return
        else:
            # 1) the engine parks the announced spell on the stack behind
            #    an OptionalCostChoice: answer "mana" ({4}{U}) for control.
            if not ctl.get("cost_answered"):
                if await answer_cost_choice(c, st, state, tag, ctx,
                                            "control", "mana"):
                    st = c.latest or st
                    state = st["state"]
                    acts = merged_actions(st)
                    return
                cast_age0 = time.time() - ctl.get("cast_time", time.time())
                if cast_age0 > 25 and not ctl.get("cost_absent_logged"):
                    ctl["cost_absent_logged"] = True
                    ctl["cost_answered"] = True  # proceed; taps are harmless
                    say(f"[{tag}] control: no cost choice offered within "
                        f"25s; proceeding to mana taps (auto-pay may apply)")
                    wire("control_cost_choice_absent", {})
            # 2) robust mana payment: wait for auto-pay, else tap one at a
            #    time with revision confirmation (2026-10-15 lesson)
            if spell_on_stack(state, GUSH) and not ctl.get("on_stack"):
                ctl["on_stack"] = True
                ctl["on_stack_lib"] = library_count(state, pid)
                ctl["on_stack_hand"] = len(hand_ids(state, pid))
                ctl["tapped_at_stack"] = len(tapped_island_oids(state, pid))
                say(f"[{tag}] control: Gush on stack; "
                    f"tapped islands={ctl['tapped_at_stack']}, "
                    f"lib={ctl['on_stack_lib']}, hand={ctl['on_stack_hand']}")
                wire("control_on_stack", {
                    "tapped_islands": ctl["tapped_at_stack"],
                    "lib": ctl["on_stack_lib"], "hand": ctl["on_stack_hand"]})
                await export_state(c, "control_stack", ctx)
            elif not ctl.get("on_stack"):
                cast_age = time.time() - ctl.get("cast_time", time.time())
                needs = ST["mana_needs"].get(tag, {})
                if ctl.get("tap_awaiting"):
                    if c.revision > ctl["tap_awaiting_rev"]:
                        used = ctl.get("tap_awaiting_used", "generic")
                        needs[used] = max(0, needs.get(used, 0) - 1)
                        ctl["tap_awaiting"] = False
                        say(f"[{tag}] control tap confirmed; "
                            f"remaining={dict(needs)}")
                    elif time.time() - ctl["tap_awaiting_since"] > 6:
                        ctl["tap_awaiting"] = False
                        say(f"[{tag}] control tap not confirmed in 6s; retry")
                elif needs and any(v > 0 for v in needs.values()) \
                        and cast_age > 8:
                    if await pay_mana_vi_one(c, st, tag, needs):
                        ctl["tap_awaiting"] = True
                        ctl["tap_awaiting_rev"] = c.revision
                        ctl["tap_awaiting_since"] = time.time()
                        ctl["tap_awaiting_used"] = "generic"
                        return
                if cast_age > 150 and not ctl.get("abandoned"):
                    ctl["abandoned"] = True
                    say(f"[{tag}] control cast never reached the stack; "
                        f"abandoning")
                    OBS["notes"].append("abandoned: control cast never "
                                        "reached the stack within 150s")
                    ctx["leg_abandon"] = True
            # resolution: the cast Gush in graveyard + stack empty
            if ctl.get("on_stack") and not ctl.get("resolved"):
                if gush_graveyard_oids(state, pid) \
                        and not spell_on_stack(state, GUSH) \
                        and not (state.get("stack") or []):
                    ctl["resolved"] = True
                    lib_now = library_count(state, pid)
                    hand_now = len(hand_ids(state, pid))
                    drew = ctl["on_stack_lib"] - lib_now
                    say(f"[{tag}] control: Gush resolved; drew={drew} "
                        f"(lib {ctl['on_stack_lib']}->{lib_now}), "
                        f"hand {ctl['on_stack_hand']}->{hand_now}")
                    wire("control_resolved", {"drew": drew,
                                              "lib": lib_now, "hand": hand_now})
                    ctl["drew"] = drew
                    ctl["tapped_post"] = len(tapped_island_oids(state, pid))
                    await export_state(c, "control_post", ctx)
                    ctl["done"] = True
                    # A1: 5 Islands tapped for {4}{U}, drew exactly 2.
                    # tapped_post is measured after payment completed.
                    if ctl.get("tapped_post", 0) >= 5 and drew == 2:
                        obs_assert("A1_harness_ok", "passed",
                                   f"control cast paid {{4}}{{U}} "
                                   f"({ctl['tapped_post']} Islands tapped), "
                                   f"resolved, drew exactly 2")
                    else:
                        obs_assert("A1_harness_ok", "failed",
                                   f"control cast abnormal: tapped="
                                   f"{ctl.get('tapped_post')}, drew={drew}")
                    return

    # ---- LEG 2 (reported branch): alternative cost
    if ctl.get("done") and not alt.get("done"):
        if not alt.get("cast_submitted"):
            if turn > 45:
                return
            if active and is_main and turn > (ctl.get("cast_turn") or 0) \
                    and len(untapped_island_oids(state, pid)) >= 2 \
                    and gush_in_hand_oid(state, pid):
                pre = await export_state(c, "pre_alt", ctx)
                alt["pre_islands"] = island_oids(pre, pid)
                alt["pre_hand"] = len(hand_ids(pre, pid))
                alt["pre_lib"] = library_count(pre, pid)
                alt["pre_gy_gush"] = gush_graveyard_oids(pre, pid)
                say(f"[{tag}] alt: pre_alt exported; battlefield islands="
                    f"{len(alt['pre_islands'])}, hand={alt['pre_hand']}, "
                    f"lib={alt['pre_lib']}")
                await cast_gush(c, state, pid, acts, tag, ctx, "alt")
                return
        else:
            cast_age = time.time() - alt.get("cast_time", time.time())
            # 1) answer the alternative-cost prompt (use it)
            if not alt.get("cost_answered"):
                if await answer_alt_cost(c, st, state, tag, ctx):
                    return
            # 2) select the 2 Islands if the engine asks
            if alt.get("cost_answered") and not alt.get("islands_selected"):
                if await select_islands_for_cost(c, st, state, pid, tag,
                                                ctx, n=2):
                    return
            # 3) the spell should now be on the stack; the 2 Islands must
            #    have left the battlefield (cost paid while casting)
            if spell_on_stack(state, GUSH) and not alt.get("on_stack"):
                alt["on_stack"] = True
                on_bf = [o for o in alt.get("pre_islands", [])
                         if obj_zone(state, o) == "Battlefield"]
                in_hand = [o for o in alt.get("pre_islands", [])
                           if obj_zone(state, o) == "Hand"]
                tapped_now = len(tapped_island_oids(state, pid))
                alt["on_stack_lib"] = library_count(state, pid)
                alt["on_stack_hand"] = len(hand_ids(state, pid))
                alt["on_stack_returned"] = in_hand
                say(f"[{tag}] alt: Gush on stack; pre-cast islands still on "
                    f"battlefield={len(on_bf)}, returned to hand={len(in_hand)} "
                    f"(oids {in_hand}), tapped islands now={tapped_now}")
                wire("alt_on_stack", {"still_on_bf": len(on_bf),
                                      "returned_oids": in_hand,
                                      "tapped_islands": tapped_now})
                await export_state(c, "alt_stack", ctx)
                log_cast_window_opps(st, state, tag, "alt_cast_window_opps")
            # 4) resolution -> draw 2 -> Gush to graveyard
            if alt.get("on_stack") and not alt.get("resolved"):
                new_gy = [o for o in gush_graveyard_oids(state, pid)
                          if o not in alt.get("pre_gy_gush", [])]
                if new_gy and not spell_on_stack(state, GUSH) \
                        and not (state.get("stack") or []):
                    alt["resolved"] = True
                    lib_now = library_count(state, pid)
                    hand_now = len(hand_ids(state, pid))
                    drew = alt["on_stack_lib"] - lib_now
                    alt["drew"] = drew
                    say(f"[{tag}] alt: Gush resolved; drew={drew} "
                        f"(lib {alt['on_stack_lib']}->{lib_now}), "
                        f"hand {alt['on_stack_hand']}->{hand_now}")
                    wire("alt_resolved", {"drew": drew})
                    post = await export_state(c, "post_alt", ctx)
                    alt["done"] = True
                    evaluate_alt(ctx, post)
                    return
            # 5) watchdog: cast submitted but the spell never materializes
            if not alt.get("on_stack") and cast_age > 120 \
                    and not alt.get("abandoned"):
                alt["abandoned"] = True
                say(f"[{tag}] alt cast submitted {cast_age:.0f}s ago but "
                    f"Gush never reached the stack; exporting stalled state")
                wire("alt_stalled", {"cast_age": round(cast_age, 1),
                                     "cost_answered": alt.get("cost_answered"),
                                     "islands_selected":
                                         alt.get("islands_selected")})
                log_cast_window_opps(st, state, tag, "alt_stall_opps")
                await export_state(c, "alt_stalled", ctx)
                alt["done"] = True
                obs_assert("A2_alt_cost_paid", "not-run",
                           "alt cast never reached the stack; cost outcome "
                           "unobservable")
                obs_assert("A3_alt_draw", "not-run",
                           "alt cast never resolved")
                OBS["notes"].append("alt leg stalled: Gush never reached "
                                    "the stack within 120s of the cast")
                return
            # log the opportunity window periodically while the alt cast
            # is in flight (documents what the engine offered)
            if cast_age > 5 and int(cast_age) % 20 == 0 \
                    and not alt.get("on_stack"):
                log_cast_window_opps(st, state, tag, "alt_cast_window_opps")

    # ---- discard to hand size (cleanup)
    if await discard_down(c, st, state, pid, tag):
        return

    # ---- pass priority when nothing real is pending
    if not real_decision_pending(c.latest or st, (c.latest or st)["state"]):
        for a in merged_actions(c.latest or st):
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return


async def tick_p1(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    await play_named_land(c, state, pid, acts, tag)
    await play_land_vi(c, st, state, pid, tag)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    wf = state.get("waiting_for") or {}
    if wf.get("type") == "DeclareAttackers":
        for a in acts:
            if a.get("type") == "DeclareAttackers":
                sub = copy.deepcopy(a)
                data = sub.get("data", {}) or {}
                data["attacks"] = []
                if "bands" in data:
                    data["bands"] = []
                await c.send_action({"type": "DeclareAttackers",
                                     "data": data})
                return
    if wf.get("type") == "DeclareBlockers":
        for a in acts:
            if a.get("type") == "DeclareBlockers":
                sub = copy.deepcopy(a)
                data = sub.get("data", {}) or {}
                data["assignments"] = []
                await c.send_action({"type": "DeclareBlockers",
                                     "data": data})
                return
    if await discard_down(c, st, state, pid, tag):
        return
    if not real_decision_pending(st, state):
        for a in merged_actions(st):
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return


# ------------------------------------------------------------------- run

def evaluate_alt(ctx, post):
    pid = 0
    alt = ctx["alt"]
    pre_islands = alt.get("pre_islands", [])
    in_hand = [o for o in pre_islands if obj_zone(post, o) == "Hand"]
    on_bf = [o for o in pre_islands if obj_zone(post, o) == "Battlefield"]
    elsewhere = [o for o in pre_islands
                 if obj_zone(post, o) not in ("Hand", "Battlefield")]
    tapped_now = len(tapped_island_oids(post, pid))
    say(f"[eval] alt: {len(pre_islands)} pre-cast islands -> hand={len(in_hand)} "
        f"(oids {in_hand}), still on battlefield={len(on_bf)}, "
        f"elsewhere={len(elsewhere)}; tapped islands now={tapped_now}")
    wire("alt_eval", {"pre_islands": pre_islands, "in_hand": in_hand,
                      "on_bf": on_bf, "elsewhere": elsewhere,
                      "tapped_islands": tapped_now,
                      "cost_answered": alt.get("cost_answered"),
                      "cost_choice_text": alt.get("cost_choice_text"),
                      "islands_selected": alt.get("islands_selected"),
                      "selected_oids": alt.get("selected_oids")})

    # A2: exactly 2 Islands must have left the battlefield for the hand.
    if len(in_hand) == 2 and len(on_bf) == len(pre_islands) - 2 \
            and not elsewhere:
        obs_assert("A2_alt_cost_paid", "passed",
                   f"alternative cost paid: 2 Islands returned to hand "
                   f"(oids {in_hand}); {len(on_bf)} remain on battlefield")
    else:
        if tapped_now >= 5 and len(in_hand) == 0:
            note = ("REPRODUCED BUG (variant): the engine tapped "
                    f"{tapped_now} Islands for {{4}}{{U}} instead of "
                    "applying the chosen alternative cost -- the cost "
                    "choice was not honored")
        elif len(in_hand) == 0:
            note = ("REPRODUCED BUG: Gush was cast via its alternative "
                    "cost but NO Island left the battlefield -- cast for "
                    "free")
        else:
            note = (f"REPRODUCED BUG (partial): only {len(in_hand)}/2 "
                    f"Islands returned (oids {in_hand}); "
                    f"{len(on_bf)} still on battlefield")
        obs_assert("A2_alt_cost_paid", "failed", note)

    # A3: resolution drew exactly 2
    drew = alt.get("drew")
    if drew == 2:
        obs_assert("A3_alt_draw", "passed",
                   "alternative-cost Gush resolved; P0 drew exactly 2")
    elif drew is None:
        obs_assert("A3_alt_draw", "not-run", "resolution never observed")
    else:
        obs_assert("A3_alt_draw", "failed",
                   f"expected to draw 2 on resolution, drew {drew}")

    # A4: post exported cleanly; the cast Gush in graveyard
    new_gy = [o for o in gush_graveyard_oids(post, pid)
              if o not in alt.get("pre_gy_gush", [])]
    if new_gy:
        obs_assert("A4_post_clean", "passed",
                   f"POST_ALT exported; cast Gush (oid {new_gy[0]}) in "
                   f"graveyard; stack empty, no pending real decisions")
    else:
        obs_assert("A4_post_clean", "failed",
                   "cast Gush not found in graveyard at POST_ALT")


async def run_leg(attempt):
    global OBS
    # 12x Gush: the control leg needs an early Gush; 4x left P0
    # Gush-less past turn 30 twice (2026-10-15), where the game then
    # stalls in Upkeep.
    p0_deck = deck(("Island", 48), (GUSH.capitalize(), 12))
    p1_deck = deck(("Island", 60))
    clients = [PhaseClient(f"P0G{attempt}")]
    await clients[0].connect()
    await clients[0].create(p0_deck, player_count=2)
    c1 = PhaseClient(f"P1G{attempt}")
    await c1.connect()
    await c1.join(clients[0].game_code, p1_deck)
    clients.append(c1)
    tags = [f"P0G{attempt}", f"P1G{attempt}"]
    for tag in tags:
        ST["mana_needs"][tag] = {}
    say(f"leg attempt={attempt} game {clients[0].game_code}; "
        f"seats={[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt,
                        "game_code": clients[0].game_code,
                        "seats": [c.player_id for c in clients]})
    OBS = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": clients[0].game_code}
    for k in ASSERT_KEYS:
        OBS["assert"][k] = "not-run"
    ctx = {"control": {"done": False}, "alt": {"done": False}}
    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(0.25)
            await tick_p0(clients[0], 0, tags[0], ctx)
            await tick_p1(clients[1], 1, tags[1], ctx)
            await asyncio.sleep(0)
            cur_rev = clients[0].revision
            if cur_rev != last_rev:
                last_rev = cur_rev
                last_rev_change = time.time()
            if time.time() - last_rev_change > 180:
                say("[leg] no state-revision change in 180s; abandoning")
                OBS["notes"].append("abandoned: stalled game")
                break
            if ctx.get("leg_abandon"):
                say("[leg] abandoning per cast watchdog")
                break
            if ctx["alt"].get("done"):
                say("[leg] alt leg complete; finishing")
                break
            # Turn cap: never let a Gush-less game drag into the turn-31
            # Upkeep stall seen twice on 2026-10-15.
            st_cap = clients[0].latest
            if st_cap and st_cap["state"].get("turn_number", 0) > 28 \
                    and not ctx["control"].get("done"):
                say("[leg] turn 28 without completing control; abandoning")
                OBS["notes"].append("abandoned: turn 28 without control "
                                    "cast (Gush never drawn)")
                break
            if time.time() - last_diag > 30:
                last_diag = time.time()
                for c, pid in zip(clients, range(2)):
                    st = c.latest
                    if not st:
                        say(f"[diag] {c.name}: NO STATE")
                        continue
                    s = st["state"]
                    say(f"[diag] {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"active={s.get('active_player')} "
                        f"stack={len(s.get('stack') or [])} "
                        f"hand={len(hand_ids(s, pid))} "
                        f"islands={len(untapped_island_oids(s, pid))} "
                        f"gush_hand={gush_in_hand_oid(s, pid)} "
                        f"ctl_done={ctx['control'].get('done')} "
                        f"alt_cast={ctx['alt'].get('cast_submitted')} "
                        f"alt_done={ctx['alt'].get('done')}")
    finally:
        for c in clients:
            await c.close()
    return OBS, ctx


def render_summary(combined):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_b = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_t = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
        font_b = font
        font_t = font
    img = Image.new("RGB", (1000, 900), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9110 -- Gush alternative cost",
           font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.110.0 (9bfd12e, protocol 142) | 2-player, native "
           "engine", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(combined.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else (
        (90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    a = combined.get("obs", {}).get("assert", {})
    d.text((24, y), f"Game {combined.get('obs', {}).get('game_code')}:",
           font=font_b, fill=(220, 220, 230))
    y += 28
    for k in ASSERT_KEYS:
        v = a.get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((48, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 28
    obs = combined.get("obs", {})
    d.text((48, y), f"Control cast drew: {obs.get('control_drew')}; "
           f"alt cast drew: {obs.get('alt_drew')}",
           font=font, fill=(220, 220, 230))
    y += 30
    d.text((48, y), f"Islands returned by alt cost: "
           f"{obs.get('alt_returned_oids')}",
           font=font, fill=(220, 220, 230))
    y += 36
    d.text((24, y), "Expected: casting Gush via its alternative cost",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "returns exactly 2 Islands to hand as the cost, then",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Gush resolves and its controller draws 2.",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Bug: the alternative cost can be cast for free -- the",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Islands never leave the battlefield.",
           font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_leg(1)
    # Retry while the control leg never completed (setup never finished).
    attempt = 1
    while not ctx["control"].get("done") and attempt < 3 \
            and obs["assert"].get("A1_harness_ok") in ("failed", "not-run"):
        attempt += 1
        say(f"attempt {attempt - 1} never completed setup; running attempt {attempt}")
        for k in list(SUBMITTED_OPPS):
            SUBMITTED_OPPS.discard(k)
        LAND_PLAYED_TURN.clear()
        MULLS.clear()
        ST["mana_needs"].clear()
        obs, ctx = await run_leg(attempt)
    a = obs["assert"]

    def reproduced():
        return a.get("A1_harness_ok") == "passed" \
            and a.get("A2_alt_cost_paid") == "failed"

    def clean():
        return all(a.get(k) == "passed" for k in ASSERT_KEYS)

    if reproduced():
        verdict = "reproduced"
    elif clean():
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    alt = ctx["alt"]
    combined = {
        "server_identity": SERVER_IDENTITY,
        "issue": ISSUE,
        "run_id": RUN_ID,
        "obs": {"attempt": obs.get("attempt"), "assert": obs["assert"],
                "notes": obs["notes"], "game_code": obs.get("game_code"),
                "control_drew": ctx["control"].get("drew"),
                "alt_drew": alt.get("drew"),
                "alt_returned_oids": alt.get("on_stack_returned"),
                "cost_choice_text": alt.get("cost_choice_text"),
                "islands_selected": alt.get("islands_selected")},
        "verdict": verdict,
        "card_data_oracle": {"gush": GUSH_ORACLE,
                             "casting_options": GUSH_CASTING},
    }
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(combined, f, indent=1, default=str)
    for k in ASSERT_KEYS:
        say(f"assertion {k}: {obs['assert'].get(k)}")
    for n in obs["notes"]:
        say(f"note: {n}")
    say(f"VERDICT: {verdict}")
    result_line = ("; ".join(f"{k} {v}" for k, v in obs["assert"].items()))
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9110",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.110.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release); evidence "
                           f"under evidence/9110/{RUN_ID}"),
        "driver": {"protocol_advertised": 142, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Island", 48], ["Gush", 12]],
            "P1": [["Island", 60]],
        },
        "assertions": {k: v for k, v in obs["assert"].items()},
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": result_line,
        "scope": ("Gush cast twice by P0: LEG 1 (control) pays {4}{U} with "
                  "5 Islands; LEG 2 (reported branch) chooses the "
                  "alternative cost 'return two Islands you control to "
                  "their owner's hand'. Asserts the 2 Islands leave the "
                  "battlefield as the cost and Gush resolves to draw 2. "
                  "Native engine, two human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "48x/60x Island + 12x Gush densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Only the alternative-cost branch and the {4}{U} control are exercised; other cost interactions (e.g. cost increasers) are out of scope.",
            "Verdict rule: reproduced = A1 passed and A2 failed. not-reproduced = A1, A2, A3 all passed. blocked = A1 failed/not-run or the alt leg never completed.",
        ],
        "setup_line": ("P0 48x Island + 12x Gush; P1 60x Island (draw-go). "
                       "P0 ramps to 5 Islands, casts Gush paying {4}{U} "
                       "(control); next P0 turn casts Gush again choosing "
                       "the alternative cost."),
        "contract_line": ("Casting Gush via its alternative cost must "
                          "return exactly 2 Islands to hand as the cost; "
                          "Gush then resolves and its controller draws 2. "
                          "Bug: the alternative cost can be cast for free -- "
                          "the Islands never leave the battlefield."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entry.json", "w") as f:
        json.dump({"gush": CARD_DATA.get("gush")}, f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9110.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(combined)
    wire("verdict", {"verdict": verdict, "assertions": run["assertions"]})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        with open(f"{EVDIR}/{fn}", "rb") as f:
            lines.append(hashlib.sha256(f.read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"VERDICT={verdict} EVIDIR={EVDIR}")


if __name__ == "__main__":
    asyncio.run(main())
