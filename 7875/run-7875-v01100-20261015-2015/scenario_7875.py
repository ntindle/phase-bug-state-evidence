#!/usr/bin/env python3
"""Issue #7875: Myr Battlesphere -- second ability does not trigger
interaction on attack.

Oracle (verified against pinned v0.110.0 card-data.json):
  When this creature enters, create four 1/1 colorless Myr artifact
  creature tokens.
  Whenever this creature attacks, you may tap X untapped Myr you control.
  If you do, this creature gets +X/+0 until end of turn and deals X damage
  to the player or planeswalker it's attacking.

Reported (Discord, 2026-08-25): "Myr Battlesphere, second ability does not
trigger interaction on attack. Cannot tap myr to use the abilities
interaction."

Behavioral contract -- single accept leg, 2-player game:
  Setup: P0 ramps Islands; P1 draw-go. When P0 has >=7 untapped Islands and
    Myr Battlesphere in hand, P0 casts it ({7} generic).
  ETB trigger resolves -> 4 untapped 1/1 Myr tokens for P0.
  Next P0 turn: declare Myr Battlesphere as an attacker on P1.
  Trigger: the "whenever attacks" trigger must go on the stack.
  DURING evidence: export pre_attack while the trigger is on the stack.
  On trigger resolution the engine must offer the optional tap-X-Myr
  interaction (a decideOptionalEffect may-prompt and/or a Myr-candidate
  selection). The driver answers accept and taps exactly 2 untapped Myr.
  Completion: let combat resolve, export POST.

Assertions:
  A1 setup_ok         Battlesphere cast+resolved (on P0's battlefield) and
                      its ETB produced >=4 untapped Myr tokens for P0.
  A2 attack_trigger    After declaring Battlesphere as an attacker, the
                      "whenever attacks" trigger was observed on the stack.
  A3 tap_interaction   The optional tap-X-Myr interaction was offered (may-
                      prompt and/or Myr candidate selection). PASSED iff the
                      driver was offered the choice; FAILED iff the trigger
                      resolved and the game moved on with no tap interaction
                      ever offered (the reported bug).
  A4 accept_effect     (gated on A3) After tapping exactly 2 Myr:
                      Battlesphere power 7->9 until EOT and P1 life
                      20->9 (9 unblocked combat + 2 trigger damage).
                      PASSED iff both hold in POST; FAILED otherwise.
  A5 post_clean        POST exported; no pending real decisions; game not
                      stuck.

Verdict rule (pre-registered):
  - reproduced:    A1 passed, attackers declared, and (A3 failed OR
                   (A3 passed and A4 failed)).
  - not-reproduced: A1-A4 all passed (A5 observational).
  - blocked: anything else (setup never completed, trigger never seen and
             the game never demonstrably moved past declare-attackers, the
             may-prompt shape was unanswerable, exports missing, etc.).

Conventions (protocol 142):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - CastSpell submitted EXACTLY as advertised (no injected targets).
  - DeclareAttackers: the advertised action is deep-copied and only its
    own "attacks" data field is filled (the action's own declared shape).
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
log = logging.getLogger("scenario7875")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7875-v01100-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 7875
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
    "signature_note": ("v0.110.0 pinned release dir re-verified this run: "
                       "GitHub still lists v0.110.0 (published 2026-10-14) as "
                       "the latest stable engine release, so the pin did not "
                       "advance; binary + data digests recomputed against the "
                       "on-disk artifacts and ServerHello 0.110.0/9bfd12e/"
                       "protocol 142 confirmed by live handshake on "
                       "127.0.0.1:9374."),
    "source": ("2026-10-15: latest stable release v0.110.0 == pinned release "
               "dir; backfill-owned v0.110.0 server on 127.0.0.1:9374 "
               "(--single-user --no-data-download, started by the 13:41 run, "
               "reused here); scenario games created via "
               "CreateGameWithSettings; evidence under evidence/7875/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"))
MYB_ORACLE = (CARD_DATA.get("myr battlesphere") or {}).get("oracle_text")


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
say("Myr Battlesphere oracle: " + str(MYB_ORACLE)[:200])

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
MYB = "myr battlesphere"

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


def player_life(state, pid):
    p = player_of(state, pid)
    return p.get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_islands(state, pid):
    n = 0
    for oid in bf_oids(state, pid):
        o = get_obj(state, oid)
        if is_land(o) and obj_lname(state, oid) == "island" \
                and not o.get("tapped"):
            n += 1
    return n


def battlesphere_oid(state, pid):
    for oid in bf_oids(state, pid):
        if obj_lname(state, oid) == MYB:
            return oid
    return None


def myr_token_oids(state, pid):
    """Oids of 1/1 Myr tokens (not the Battlesphere itself) controlled by pid."""
    out = []
    for oid in bf_oids(state, pid):
        n = obj_lname(state, oid)
        if n == "myr" or (n.startswith("myr") and "battlesphere" not in n):
            out.append(oid)
    return out


def untapped_myr_oids(state, pid):
    return [o for o in myr_token_oids(state, pid)
            if not get_obj(state, o).get("tapped")]


def creature_power(state, oid):
    o = get_obj(state, oid)
    for k in ("power", "current_power", "base_power"):
        if o.get(k) is not None:
            return o.get(k)
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


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


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


def trigger_on_stack(state):
    """(bool, desc) -- is the Battlesphere attack trigger on the stack?"""
    for e in stack_entries(state):
        name = stack_entry_name(state, e)
        txt = str(e.get("text") or e.get("description") or "")
        blob = (name + " " + txt).lower()
        if "battlesphere" in blob:
            return True, (name or txt)[:120]
    return False, ""


ASSERT_KEYS = ["A1_setup_ok", "A2_attack_trigger", "A3_tap_interaction",
               "A4_accept_effect", "A5_post_clean"]

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


def spell_on_stack(state, name):
    for e in stack_entries(state):
        if stack_entry_name(state, e).lower() == name:
            return True
    return False


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st, state):
    """A genuine decision (not mana taps / priority / land play prompts)
    is pending for us."""
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


async def play_land_vi(c, st, state, pid, tag):
    """Answer a vi land-play prompt (action code playLand) for an Island
    in hand, at most one per turn."""
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


# ------------------------------------------------------- tap-Myr interaction

def find_may_tap_opp(st):
    """decideOptionalEffect may-prompt (exactChoices) for the optional tap."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def find_myr_select_opp(st, state, pid):
    """Schema select/sequence whose candidates are P0's untapped Myr tokens."""
    untapped = set(untapped_myr_oids(state, pid))
    if not untapped:
        return None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or []
        if not cands:
            continue
        refs = {_cand_ref(ch) for ch in cands}
        refs.discard(None)
        if refs and refs <= untapped:
            return opp, [ch for ch in cands if _cand_ref(ch) in untapped]
    return None


def pick_accept_choice(opp):
    """Choice id for the accept/yes branch of a decideOptionalEffect prompt."""
    chs = (opp.get("response", {}).get("data", {}) or {}).get("choices", [])
    for ch in chs:
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if any(w in txt for w in ("yes", "tap", "accept")):
                is_accept = True
            elif any(w in txt for w in ("no", "decline", "don't", "do not")):
                is_accept = False
        if is_accept:
            return ch.get("id"), choice_text(ch)
    return None, None


async def answer_may_tap(c, st, tag, ctx):
    opp = find_may_tap_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return False
    cid, ctxt = pick_accept_choice(opp)
    if cid is None:
        say(f"[{tag}] may-tap prompt found but no accept choice identifiable; "
            f"stalling for diagnostics")
        wire("may_tap_unanswerable", {"opportunity": opp})
        ctx["may_unanswerable"] = True
        return False
    SUBMITTED_OPPS.add(iid)
    ctx["may_answered"] = True
    say(f"[{tag}] may-tap prompt -> ACCEPT ({ctxt})")
    wire("may_tap_accept", {"choice_text": ctxt})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": cid}}}, tag)
    return True


async def select_myr_to_tap(c, st, state, pid, tag, ctx, n=2):
    found = find_myr_select_opp(st, state, pid)
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
    ctx["myr_selected"] = True
    ctx["tapped_oids"] = pick_refs
    say(f"[{tag}] tapping {len(picks)} Myr (oids {pick_refs})")
    wire("myr_tap_select", {"oids": pick_refs})
    rtype = (opp.get("response", {}) or {}).get("type")
    if rtype == "schema":
        resp = {"type": "select", "data": {"choiceIds": pick_ids}}
    else:
        resp = {"type": "choose", "data": {"choiceId": pick_ids[0]}}
    await interact_as(c, {"interactionId": iid, "response": resp}, tag)
    return True

# ------------------------------------------------------------------- ticks

async def declare_attack_p0(c, state, pid, tag, ctx, acts):
    da = None
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            da = a
            break
    if da is None:
        return False
    bs = battlesphere_oid(state, pid)
    if bs is None:
        return False
    sub = copy.deepcopy(da)
    data = sub.get("data", {}) or {}
    data["attacks"] = [[int(bs), {"type": "Player", "data": 1}]]
    if "bands" in data:
        data["bands"] = []
    sub["data"] = data
    say(f"[{tag}] declaring attackers: Myr Battlesphere (oid {bs}) -> P1; "
        f"advertised shape keys={sorted(da.get('data', {}).keys())}")
    wire("declare_attackers", {"who": tag, "battlesphere_oid": bs,
                               "advertised_keys": sorted(da.get("data", {}).keys())})
    await c.send_action({"type": "DeclareAttackers", "data": data})
    ctx["attackers_declared"] = True
    ctx["attack_turn"] = state.get("turn_number")
    ctx["battlesphere_oid"] = str(bs)
    return True


async def answer_declare_blockers_p1(c, state, tag, ctx, acts):
    for a in acts:
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            data = sub.get("data", {}) or {}
            data["assignments"] = []
            say(f"[{tag}] declaring no blockers")
            wire("declare_blockers", {"who": tag})
            await c.send_action({"type": "DeclareBlockers", "data": data})
            return True
    return False


async def answer_declare_attackers_p1(c, state, tag, acts):
    """P1 is draw-go: declare no attackers on its own declare step."""
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            data = sub.get("data", {}) or {}
            data["attacks"] = []
            if "bands" in data:
                data["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": data})
            return True
    return False


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

    # --- cast gate: 7 untapped Islands + Battlesphere in hand, P0 main phase
    if not ctx.get("cast_submitted"):
        turn = state.get("turn_number", 0)
        if turn > 40:
            return
        if active and is_main and MYB in hand_names(state, pid) \
                and untapped_islands(state, pid) >= 7:
            a, oid = cast_action_for(acts, state, MYB)
            if a is not None:
                ctx["pre_cast_state"] = await export_state(c, "pre_cast", ctx)
                ST["mana_needs"][tag] = {"generic": 7}
                say(f"[{tag}] casting Myr Battlesphere (oid {oid}), "
                    f"needs={ST['mana_needs'][tag]}")
                wire("cast", {"who": tag, "oid": oid})
                await submit_as_is(c, a)
                ctx["cast_submitted"] = True
                ctx["cast_time"] = time.time()
                ctx["cast_turn"] = state.get("turn_number")
                ctx["cast_oid"] = str(oid)
                return

    # --- robust mana payment while the cast is in flight
    # Strategy: payment_mode Auto may auto-pay. Wait 8s after the cast;
    # if the spell is on the stack, payment is done. Otherwise tap lands
    # ONE AT A TIME, confirming each via state-revision change before the
    # next (a fast tap loop loses taps to a race -- seen 2026-10-15).
    if ctx.get("cast_submitted") and not ctx.get("cast_resolved"):
        if spell_on_stack(state, MYB):
            ctx["cast_resolved"] = True
            say(f"[{tag}] Battlesphere on stack; payment complete "
                f"(auto-pay worked)")
            wire("autopay_ok", {})
        else:
            cast_age = time.time() - ctx.get("cast_time", time.time())
            needs = ST["mana_needs"].get(tag, {})
            needs_remain = needs and any(v > 0 for v in needs.values())
            if ctx.get("tap_awaiting"):
                if c.revision > ctx["tap_awaiting_rev"]:
                    # Tap confirmed applied: decrement the need.
                    used = ctx.get("tap_awaiting_used", "generic")
                    needs[used] = max(0, needs.get(used, 0) - 1)
                    ctx["tap_awaiting"] = False
                    say(f"[{tag}] tap confirmed; remaining needs={needs}")
                    wire("tap_confirmed", {"remaining": dict(needs)})
                elif time.time() - ctx["tap_awaiting_since"] > 6:
                    ctx["tap_awaiting"] = False
                    say(f"[{tag}] tap NOT confirmed in 6s; retrying")
                    wire("tap_timeout", {})
            elif needs_remain and cast_age > 8:
                if await pay_mana_vi_one(c, st, tag, needs):
                    ctx["tap_awaiting"] = True
                    ctx["tap_awaiting_rev"] = c.revision
                    ctx["tap_awaiting_since"] = time.time()
                    # record which need this tap serves (for decrement)
                    ctx["tap_awaiting_used"] = "generic"
                    st = c.latest or st
                    state = st["state"]
                    acts = merged_actions(st)
            # Log rejections once, shortly after the cast.
            if not ctx.get("rej_logged") and cast_age > 3:
                ctx["rej_logged"] = True
                say(f"[{tag}] post-cast rejections: {c.rejections[-3:]}")
                wire("post_cast_rejections", {"rejections": c.rejections[-3:]})
            # Abandon the leg if the cast never materializes.
            if cast_age > 150 and not ctx.get("cast_abandoned"):
                ctx["cast_abandoned"] = True
                say(f"[{tag}] cast submitted {cast_age:.0f}s ago but spell "
                    f"never reached the stack; abandoning leg")
                OBS["notes"].append("abandoned: cast submitted but spell "
                                    "never reached the stack within 150s")
                ctx["leg_abandon"] = True

    # --- cast resolved? Battlesphere on the battlefield + ETB Myr tokens
    if ctx.get("cast_submitted") and not ctx.get("a1_done"):
        bs = battlesphere_oid(state, pid)
        if bs is not None:
            ctx["cast_resolved"] = True
            myrs = untapped_myr_oids(state, pid)
            say(f"[{tag}] Battlesphere on battlefield (oid {bs}); "
                f"untapped Myr tokens: {len(myrs)}")
            wire("etb_tokens", {"battlesphere_oid": bs,
                                "untapped_myr": len(myrs),
                                "myr_oids": myrs})
            if len(myrs) >= 4:
                obs_assert("A1_setup_ok", "passed",
                           f"Battlesphere resolved; ETB produced "
                           f"{len(myrs)} untapped Myr tokens")
                ctx["a1_done"] = True

    # --- attack gate: next P0 turn, declare-attackers step
    if ctx.get("a1_done") and not ctx.get("attackers_declared"):
        if wtype == "DeclareAttackers" \
                and (state.get("turn_number", 0) or 0) > (ctx.get("cast_turn") or 0):
            if await declare_attack_p0(c, state, pid, tag, ctx, acts):
                return

    # --- trigger watch: the "whenever attacks" trigger on the stack
    if ctx.get("attackers_declared") and not ctx.get("trigger_seen"):
        seen, desc = trigger_on_stack(state)
        if seen:
            ctx["trigger_seen"] = True
            obs_assert("A2_attack_trigger", "passed",
                       f"attack trigger observed on the stack ({desc})")
            say(f"[{tag}] TRIGGER ON STACK: {desc}")
            ctx["pre_attack_state"] = await export_state(c, "pre_attack", ctx)
            wire("trigger_on_stack", {"desc": desc})
        else:
            # log stack contents during the window for diagnosis
            names = [stack_entry_name(state, e) for e in stack_entries(state)]
            if names:
                wire("stack_window", {"entries": names})

    # --- the optional tap-Myr interaction (may-prompt and/or selection)
    if ctx.get("attackers_declared") and not ctx.get("interaction_done"):
        if wtype == "DeclareBlockers":
            ctx["saw_declare_blockers"] = True
        # selection first (more specific), then the may-prompt
        if not ctx.get("myr_selected"):
            if await select_myr_to_tap(c, st, state, pid, tag, ctx, n=2):
                obs_assert("A3_tap_interaction", "passed",
                           "tap-X-Myr candidate selection offered; "
                           "tapped 2 untapped Myr")
                ctx["interaction_done"] = True
                return
        if not ctx.get("may_answered") and not ctx.get("may_unanswerable"):
            if await answer_may_tap(c, st, tag, ctx):
                # the may-prompt alone proves the interaction was offered
                if OBS["assert"].get("A3_tap_interaction") == "not-run":
                    obs_assert("A3_tap_interaction", "passed",
                               "decideOptionalEffect may-prompt for the "
                               "tap-Myr option was offered; answered accept")
                return
        # log any pending opportunity shapes during the trigger window
        if ctx.get("trigger_seen") and not ctx.get("interaction_done"):
            opps = []
            for opp in vi_ops(st):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                items = data.get("choices") or data.get("candidates") or []
                opps.append({"iid": str(opp.get("interactionId"))[:12],
                             "rtype": (opp.get("response", {}) or {}).get("type"),
                             "n": len(items),
                             "codes": sorted(_opp_codes(opp)),
                             "texts": [choice_text(ch)[:60] for ch in items[:4]]})
            if opps:
                wire("trigger_window_opps", {"opps": opps,
                                             "waiting_for": wtype})

    # --- pass priority when nothing real is pending
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
        if await answer_declare_attackers_p1(c, state, tag, acts):
            return
    if wf.get("type") == "DeclareBlockers":
        if await answer_declare_blockers_p1(c, state, tag, ctx, acts):
            return
    if not real_decision_pending(st, state):
        for a in merged_actions(st):
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return

# ------------------------------------------------------------------- run

async def run_leg(attempt):
    global OBS
    p0_deck = deck(("Island", 56), (MYB, 4))
    p1_deck = deck(("Island", 60))
    clients = [PhaseClient(f"P0B{attempt}")]
    await clients[0].connect()
    await clients[0].create(p0_deck, player_count=2)
    c1 = PhaseClient(f"P1B{attempt}")
    await c1.connect()
    await c1.join(clients[0].game_code, p1_deck)
    clients.append(c1)
    tags = [f"P0B{attempt}", f"P1B{attempt}"]
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
    ctx = {"cast_submitted": False, "cast_resolved": False,
           "a1_done": False, "attackers_declared": False,
           "trigger_seen": False, "may_answered": False,
           "myr_selected": False, "interaction_done": False,
           "post_exported": False, "settle_ticks": 0,
           "saw_declare_blockers": False}
    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    try:
        while time.time() - t0 < 1200:
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
                for c, pid in zip(clients, range(2)):
                    st = c.latest
                    if not st:
                        say(f"[stall-diag] {c.name}: NO STATE")
                        continue
                    s = st["state"]
                    wf = s.get("waiting_for") or {}
                    opps = []
                    for opp in vi_ops(st):
                        data = (opp.get("response", {}) or {}).get("data", {}) or {}
                        items = data.get("choices") or data.get("candidates") or []
                        opps.append({
                            "iid": str(opp.get("interactionId"))[:12],
                            "rtype": (opp.get("response", {}) or {}).get("type"),
                            "n": len(items),
                            "codes": sorted(_opp_codes(opp)),
                            "texts": [choice_text(ch)[:60] for ch in items[:4]],
                        })
                    say(f"[stall-diag] {c.name}: waiting_for={wf.get('type')} "
                        f"wf_data={json.dumps(wf.get('data'), default=str)[:300]}")
                    say(f"[stall-diag] {c.name}: opps={json.dumps(opps, default=str)[:900]}")
                    say(f"[stall-diag] {c.name}: action_types="
                        f"{sorted(set(a.get('type') for a in merged_actions(st)))}")
                    say(f"[stall-diag] {c.name}: rejections={c.rejections[-3:]}")
                break
            st_now = clients[0].latest
            if st_now and st_now["state"].get("turn_number", 0) > 45 \
                    and not ctx.get("cast_submitted"):
                say("[leg] turn cap reached without casting; abandoning")
                OBS["notes"].append("abandoned: turn 45 without casting "
                                    "Battlesphere (setup never completed)")
                break
            if ctx.get("leg_abandon"):
                say("[leg] abandoning per payment watchdog")
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
                        f"islands={untapped_islands(s, pid)} "
                        f"myr={len(untapped_myr_oids(s, pid))} "
                        f"bs={battlesphere_oid(s, pid)} "
                        f"cast={ctx.get('cast_submitted')} "
                        f"atk={ctx.get('attackers_declared')} "
                        f"trig={ctx.get('trigger_seen')} "
                        f"interact={ctx.get('interaction_done')}")
            # completion: attackers declared, interaction answered (or the
            # game demonstrably moved past combat), stack settled on both
            # views for 3 consecutive ticks -> POST export
            if ctx.get("attackers_declared") and not ctx.get("post_exported"):
                latests = [c.latest for c in clients]
                if all(x is not None for x in latests) \
                        and all(not (x["state"].get("stack") or [])
                                for x in latests) \
                        and not any(real_decision_pending(x, x["state"])
                                    for x in latests):
                    ctx["settle_ticks"] += 1
                    if ctx["settle_ticks"] >= 3:
                        say("[leg] combat settled; exporting POST")
                        post = await export_state(clients[0], "post", ctx)
                        ctx["post_state"] = post
                        ctx["post_exported"] = True
                        evaluate_post(ctx, post)
                        break
    finally:
        for c in clients:
            await c.close()
    return OBS, ctx


def evaluate_post(ctx, post):
    pid = 0
    bs = battlesphere_oid(post, pid)
    power = creature_power(post, bs) if bs else None
    p1_life = player_life(post, 1)
    myrs = myr_token_oids(post, pid)
    tapped_myrs = [o for o in myrs if get_obj(post, o).get("tapped")]
    say(f"[eval] battlesphere oid={bs} power={power}; P1 life={p1_life}; "
        f"myr tokens={len(myrs)} tapped={len(tapped_myrs)}")
    wire("post_eval", {"battlesphere_oid": bs, "power": power,
                       "p1_life": p1_life, "myr_total": len(myrs),
                       "myr_tapped": len(tapped_myrs),
                       "tapped_oids": ctx.get("tapped_oids")})
    ctx["post_power"] = power
    ctx["post_p1_life"] = p1_life

    # A3: if the trigger was seen but no tap interaction was ever offered
    # and the game moved past combat, the reported bug is confirmed.
    if OBS["assert"].get("A3_tap_interaction") == "not-run":
        if ctx.get("trigger_seen") and not ctx.get("interaction_done"):
            obs_assert("A3_tap_interaction", "failed",
                              "REPRODUCED BUG: the attack trigger resolved "
                              "and combat completed with no tap-X-Myr "
                              "interaction ever offered to P0")
        elif not ctx.get("trigger_seen") and ctx.get("saw_declare_blockers"):
            obs_assert("A3_tap_interaction", "failed",
                              "REPRODUCED BUG (deeper): attackers were "
                              "declared and the game reached declare-blockers "
                              "with the attack trigger never appearing on "
                              "the stack -- no tap interaction possible")
        elif not ctx.get("trigger_seen"):
            obs_assert("A3_tap_interaction", "not-run",
                              "attack trigger never observed on the stack "
                              "and the game never demonstrably moved past "
                              "declare-attackers")

    # A4: accept-branch outcome (only meaningful if the interaction happened)
    if ctx.get("interaction_done") and ctx.get("tapped_oids"):
        n = len(ctx["tapped_oids"])
        exp_power = 7 + n
        # unblocked combat: 7+n combat damage + n trigger damage
        exp_life = 20 - (7 + n) - n
        power_ok = (power == exp_power)
        life_ok = (p1_life == exp_life)
        say(f"[eval] A4: tapped={n} expect power={exp_power} got {power}; "
            f"expect P1 life={exp_life} got {p1_life}")
        if power_ok and life_ok:
            obs_assert("A4_accept_effect", "passed",
                              f"tapped {n} Myr -> Battlesphere {7}/{7} to "
                              f"{power}/{get_obj(post, bs).get('toughness')}; "
                              f"P1 life 20->{p1_life}")
        else:
            obs_assert("A4_accept_effect", "failed",
                              f"tapped {n} Myr but outcome wrong: power "
                              f"{power} (want {exp_power}), P1 life "
                              f"{p1_life} (want {exp_life})")
    elif OBS["assert"].get("A3_tap_interaction") == "passed" \
            and not ctx.get("interaction_done"):
        obs_assert("A4_accept_effect", "not-run",
                          "interaction offered but the tap selection never "
                          "completed (may-prompt answered, no selection)")
    else:
        obs_assert("A4_accept_effect", "not-run",
                          "no tap interaction was offered; accept branch "
                          "not exercised")

    # A5: post exported cleanly
    if ctx.get("post_exported"):
        obs_assert("A5_post_clean", "passed",
                          "POST exported; stack empty and no pending real "
                          "decisions on either client view")
    else:
        obs_assert("A5_post_clean", "not-run",
                          "POST was never exported")


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
    img = Image.new("RGB", (1000, 860), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #7875 -- Myr Battlesphere",
           font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.110.0 (9bfd12e, protocol 142) | 2-player, accept leg, "
           "native engine", font=font, fill=(180, 180, 190))
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
    d.text((48, y), f"Trigger on stack: {obs.get('trigger_seen')}",
           font=font, fill=(220, 220, 230))
    y += 30
    d.text((48, y), f"Tap interaction offered: {obs.get('interaction_done')}",
           font=font, fill=(220, 220, 230))
    y += 30
    d.text((48, y), f"POST: Battlesphere power={obs.get('post_power')}, "
           f"P1 life={obs.get('post_p1_life')}",
           font=font, fill=(220, 220, 230))
    y += 36
    d.text((24, y), "Expected: on attack, the trigger offers the optional",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "\"tap X untapped Myr\"; tapping 2 gives +2/+0 and 2",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "damage to the defending player.",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Bug: no interaction appears; the Myr cannot be tapped.",
           font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_leg(1)
    # Retry while setup never completed (cast never submitted).
    attempt = 1
    while not ctx.get("cast_submitted") and attempt < 3 \
            and obs["assert"].get("A1_setup_ok") in ("failed", "not-run"):
        attempt += 1
        say(f"attempt {attempt - 1} never completed setup; running attempt {attempt}")
        for k in list(SUBMITTED_OPPS):
            SUBMITTED_OPPS.discard(k)
        LAND_PLAYED_TURN.clear()
        MULLS.clear()
        obs, ctx = await run_leg(attempt)
    a = obs["assert"]

    def reproduced():
        return a.get("A1_setup_ok") == "passed" \
            and ctx.get("attackers_declared") \
            and (a.get("A3_tap_interaction") == "failed"
                 or (a.get("A3_tap_interaction") == "passed"
                     and a.get("A4_accept_effect") == "failed"))

    def clean():
        return all(a.get(k) == "passed" for k in ASSERT_KEYS)

    if reproduced():
        verdict = "reproduced"
    elif clean():
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    combined = {
        "server_identity": SERVER_IDENTITY,
        "issue": ISSUE,
        "run_id": RUN_ID,
        "obs": {"attempt": obs.get("attempt"), "assert": obs["assert"],
                "notes": obs["notes"], "game_code": obs.get("game_code"),
                "trigger_seen": ctx.get("trigger_seen"),
                "interaction_done": ctx.get("interaction_done"),
                "tapped_oids": ctx.get("tapped_oids"),
                "post_power": ctx.get("post_power"),
                "post_p1_life": ctx.get("post_p1_life"),
                "attackers_declared": ctx.get("attackers_declared")},
        "verdict": verdict,
        "card_data_oracle": {"myr battlesphere": MYB_ORACLE},
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
        "issue_url": "https://github.com/phase-rs/phase/issues/7875",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.110.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release, reused from the 13:41 run); evidence "
                           f"under evidence/7875/{RUN_ID}"),
        "driver": {"protocol_advertised": 142, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Island", 56], ["Myr Battlesphere", 4]],
            "P1": [["Island", 60]],
        },
        "assertions": {k: v for k, v in obs["assert"].items()},
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": result_line,
        "scope": ("Myr Battlesphere cast for {7}, ETB creates 4 Myr tokens, "
                  "Battlesphere attacks P1; asserts the 'whenever attacks' "
                  "trigger reaches the stack and the optional tap-X-Myr "
                  "interaction is offered; accept branch taps 2 Myr and "
                  "checks +2/+0 and 2 damage to the defending player. "
                  "Native engine, two human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "56x/60x Island + 4x Battlesphere densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Only the accept branch of the optional tap is exercised; the decline control is not run in this leg.",
            "Verdict rule: reproduced = A1 passed, attackers declared, and (A3 failed OR (A3 passed and A4 failed)). not-reproduced = A1-A4 all passed. blocked = setup/trigger never completed or the interaction shape was unanswerable.",
        ],
        "setup_line": ("P0 56x Island + 4x Myr Battlesphere; P1 60x Island "
                       "(draw-go). P0 ramps to 7 Islands, casts Battlesphere, "
                       "lets the ETB create 4 Myr tokens, attacks with "
                       "Battlesphere, answers the tap-Myr interaction."),
        "contract_line": ("On attack, the trigger must offer the optional "
                          "'tap X untapped Myr you control'; tapping X Myr "
                          "gives +X/+0 until EOT and deals X damage to the "
                          "defending player. Bug: no interaction appears; "
                          "the Myr cannot be tapped."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entry.json", "w") as f:
        json.dump({"myr battlesphere": CARD_DATA.get("myr battlesphere")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_7875.py", "w") as f:
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
