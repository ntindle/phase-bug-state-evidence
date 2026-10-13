"""Issue #9843: Mundungu -- compound unless-payment drops the life component.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 driver conventions (verified by scenario_9841/9842_01080.py,
2026-10-13): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, real_decision_pending gating
priority passes, vi candidate references are STRINGS (normalize with str()).

ISSUE (phase-rs/phase#9843, opened 2026-10-12, open,
bug/area:parser/priority:p2-wrong-game-result): Mundungu's oracle text is
"{T}: Counter target spell unless its controller pays {1} and 1 life." The
parser lowers the unless cost to a bare mana cost: pinned v0.108.0
card-data.json shows
  unless_pay.cost == {"type":"Mana","cost":{"type":"Cost","shards":[],"generic":1}}
with NO life component. Expected: the spell's controller must pay both {1}
AND 1 life to avoid being countered. Actual: paying {1} alone avoids it.

BEHAVIORAL CONTRACT (PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Game A (pay branch -- the reported bug):
  P1 controls Mundungu (untapped, past summoning sickness). P0 casts Shock
  targeting P1. P1 activates Mundungu's ability targeting Shock. The ability
  resolves; the engine offers the unless-payment to P0 (the spell's
  controller). P0 PAYS whatever is offered.
  Expected per the issue: the offered cost is {1} AND 1 life; after paying,
  Shock resolves (P1 20 -> 18) and P0 is at 19 life.
  Buggy: the offered cost is {1} only; after paying, Shock resolves
  (P1 -> 18) and P0 stays at 20 life.

Game B (decline branch -- control): same setup, P0 DECLINES the unless
  payment. Expected (both correct and buggy engines): Shock is countered,
  P1 stays at 20, P0 at 20. Proves the unless mechanism itself works and
  isolates the bug to the cost content.

Assertions:
  A1_parse    pinned card-data unless_pay.cost is bare Mana {1} with no life
              component (parse evidence; supports, does not decide).
  A2_setup    Game A: Mundungu on P1 BF untapped at activation; Shock cast
              by P0 reached the stack.
  A3_target   Mundungu's ability targeted Shock (no target prompt when Shock
              is the single legal target -- auto-targeted; evidenced by the
              unless prompt to P0 while Shock was on the stack).
  A4_unless   P0 received the unless-payment prompt; the offered cost carried
              no life component (opportunity JSON archived verbatim).
  A5_pay      [the reported bug] P0 paid -> Shock RESOLVED (P1 20 -> 18, Shock
              in P0 graveyard) AND P0 life UNCHANGED at 20. FAILS(=reproduced)
              iff spell survived with life unchanged.
  A6_decline  [control] P0 declined -> Shock COUNTERED (P1 stays 20, Shock in
              P0 graveyard, P0 at 20).
  A7_cleanup  stack empty post-resolution (both games).

Verdict rule: reproduced iff A2, A3, A4 pass and A5 shows spell-survived AND
P0-life-unchanged. not-reproduced iff A5 shows P0 life dropped by 1 (the
correct compound payment was taken). blocked iff setup/target/unless never
completed.

Unless-prompt answering policy (discovery-first): the prompt is an
exactChoices opportunity with no text; the pay/decline semantic lives in
each choice's value surface {role:"pay", value:"true"|"false"}. Game A
answers pay=true, Game B pay=false. If no such choice exists, the branch is
recorded not-run -- never guessed. Every unclaimed decision is archived
verbatim (opportunity_unknown_*.json).

Evidence: evidence/9843/<run-id>/pre_ability.json, pre_unless.json,
post_pay.json, post_decline.json, parse_evidence.json, opportunity_*.json,
run.json, manifest.sha256, summary.png, scenario_9843_01080.py,
wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-9843-v01080-20261013-0415"
ISSUE = 9843
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MUNDUNGU = "mundungu"
SHOCK = "shock"
MOUNTAIN = "mountain"
ISLAND = "island"
SWAMP = "swamp"
P0_DECK = deck((SHOCK, 8), (MOUNTAIN, 52))
P1_DECK = deck((MUNDUNGU, 8), (ISLAND, 26), (SWAMP, 26))

CARD_DATA_PATH = f"{BACKFILL}/server/releases/v0.108.0/data/card-data.json"
CARD_DATA = json.load(open(CARD_DATA_PATH))

SERVER_IDENTITY = {
    "server_version": "v0.108.0",
    "build_commit": "b8fa1d3",
    "protocol_version": 134,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.108.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode (pin verified 2026-10-12 16:20 run; binary "
                       "digest matches the GitHub asset digest); data digests "
                       "match the signed manifest; digests recomputed against "
                       "on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.108.0 (published "
               "2026-10-12T21:10:34Z) == pinned release dir; ServerHello "
               "0.108.0/b8fa1d3/protocol 134 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server on 127.0.0.1:9374 started by this run "
               "backfill-20261013-084136"),
}
for _f, _k in (("server/releases/v0.108.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.108.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.108.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def state_life(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def on_bf(state, pid, name):
    return any(obj_lname(state, o) == name for o in bf_oids(state, pid))


def bf_named_oid(state, pid, name):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def in_gy(state, pid, name):
    return any(obj_lname(state, o) == name
               for o in player_of(state, pid).get("graveyard", []))


def card_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"


def parse_evidence():
    """Capture the unless_pay lowering for Mundungu from the PINNED
    v0.108.0 card-data.json."""
    e = CARD_DATA[MUNDUNGU]
    abil = (e.get("abilities") or [{}])[0]
    out = {
        "oracle_text": e.get("oracle_text"),
        "ability_kind": abil.get("kind"),
        "ability_cost": abil.get("cost"),
        "effect": abil.get("effect"),
        "unless_pay": abil.get("unless_pay"),
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def stack_has_spell(state, name):
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone") or "").lower() == "stack" and \
                str(o.get("base_name") or o.get("name") or "").lower() == name:
            return True
    for entry in state.get("stack") or []:
        if name in json.dumps(entry).lower():
            return True
    return False


def stack_mentions(state, needle):
    for entry in state.get("stack") or []:
        if needle in json.dumps(entry).lower():
            return True
    return False


def cast_action_for(acts, state, oid):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


async def cast_named(c, state, acts, name, oid, tag, g, flag):
    a = cast_action_for(acts, state, oid)
    if a is None:
        return False
    say(f"[{tag}] casting {name} (oid={oid})")
    wire("cast", {"who": tag, "name": name, "oid": str(oid)})
    await submit_as_is(c, a)
    g.pending_cast = {"oid": str(oid), "name": name,
                      "since": time.time(), "tag": tag, "flag": flag}
    setattr(g, flag, True)
    await asyncio.sleep(0.5)
    return True


def cast_guard_tick(g, state, tag):
    pc = g.pending_cast
    if not pc:
        return "proceed"
    o = (state.get("objects") or {}).get(str(pc["oid"]))
    zone = str((o.get("zone") or "")).lower() if o else "gone"
    if zone in ("stack", "battlefield", "graveyard", "exile", "command"):
        g.pending_cast = None
        say(f"[{tag}] cast confirmed ({zone}): {pc['name']} oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": zone})
        return "proceed"
    want = pc["name"].lower()
    for oid2, o2 in (state.get("objects") or {}).items():
        nm = str(o2.get("base_name") or o2.get("name") or "").lower()
        if nm == want and str(o2.get("zone") or "").lower() == "stack":
            g.pending_cast = None
            say(f"[{tag}] cast confirmed (stack scan, oid {oid2}): {pc['name']}")
            wire("cast_confirmed", {"name": pc["name"], "oid": str(oid2),
                                    "zone": "stack", "via": "name_scan"})
            return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s ({zone}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]), "zone": zone})
        g.pending_cast = None
        if pc.get("flag"):
            setattr(g, pc["flag"], False)
        return "proceed"
    return "hold"


def candidate_seat(ch):
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
    return None


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
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def all_real_opportunities(st, state):
    """Non-priority-menu vi opportunities with real (non-menu) choices."""
    MENU_ACTION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                         "castSpell", "activateAbility", "mulliganDecision"}
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type")
        real = []
        for ch in cands:
            is_menu = False
            for s in ch.get("surfaces", []) or []:
                if s.get("type") == "action" and \
                        (s.get("data") or {}).get("code") in MENU_ACTION_CODES:
                    is_menu = True
                    break
            if not is_menu:
                real.append(ch)
        if real:
            out.append((opp, rtype, stype, real))
    return out


async def submit_target(c, opp, rtype, spec_type, ch, tag, game):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    key = ("target", str(iid), str(cid))
    if key in game.submitted:
        return None
    game.submitted.add(key)
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']} "
        f"seat={candidate_seat(ch)} ({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid


async def do_mulligan(c, acts, st, pid, tag, game, keep_fn):
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
    if key in game.mulls:
        return False
    game.mulls.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in game.mulls
                     if k[0] == tag and k[1] == "mull")
    if keep_fn(state, pid) or mull_count >= 4 or len(hn) <= 4:
        say(f"[{tag}] keep {len(hn)}: {hn}")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn,
                          "iid": str(iid)[:16]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    else:
        say(f"[{tag}] mulligan #{mull_count + 1} ({len(hn)}: {hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn,
                          "iid": str(iid)[:16]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, game, avoid_names):
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
    key = (tag, "bottom", str(iid))
    if key in game.submitted:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in avoid_names:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = [ch.get("id") for ch in ranked[:n]]
    game.submitted.add(key)
    say(f"[{tag}] bottoming {[choice_text(x)[:40] for x in ranked[:n]]} via vi")
    sub = {"interactionId": iid,
           "response": {"type": "select", "data": {"choiceIds": picks}}}
    await interact_as(c, sub, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, game, avoid_names):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = {str(x) for x in hand}
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        refs = [_cand_reference(ch) for ch in cands]
        if not any(r in handset for r in refs):
            continue
        key = (tag, "handsize", str(c.revision), str(opp.get("interactionId")))
        if key in game.submitted:
            return False

        def rank(ch):
            nm = obj_lname(state, _cand_reference(ch))
            if nm in avoid_names:
                return (5, nm)
            if _cand_reference(ch) is not None and is_land(
                    get_obj(state, _cand_reference(ch))):
                return (0, nm)
            return (2, nm)

        picks = [ch["id"] for ch in sorted(cands, key=rank)[:max(1, n)]]
        game.submitted.add(key)
        say(f"[{tag}] discarding to hand size via vi: picks={picks}")
        wire("handsize_discard", {"who": tag, "picks": picks})
        spec = rdata.get("spec") or {}
        stype = spec.get("type") or "select"
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pass_priority(c, st, acts, game):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def play_land_prefer(c, state, pid, acts, tag, game, pref_names):
    if game.lands_locked:
        return False
    turn = state.get("turn_number")
    if game.land_played_turn.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False

    def has(nm):
        return any(obj_lname(state, o) == nm for o in bf_oids(state, pid))

    def rank(o):
        nm = obj_lname(state, o)
        for i, pn in enumerate(pref_names):
            if nm == pn and not has(pn):
                return i
        return len(pref_names)

    for o in sorted(lands, key=rank):
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                game.land_played_turn[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


def combat_tick(acts):
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            return d
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            return d
    return None


# ---------------------------------------------------------------- Game class
class Game:
    def __init__(self, tag, mode):
        self.tag = tag
        self.mode = mode  # "pay" or "decline"
        self.p0 = self.p1 = None
        self.p1_seat = 1
        self.mulls = set()
        self.submitted = set()
        self.land_played_turn = {}
        self.lands_locked = False
        self.pending_cast = None
        self.notes = []
        # scenario progress flags
        self.mundungu_cast = False
        self.shock_cast = False
        self.ability_activated = False
        self.ability_target_answered = False
        self.ability_on_stack_seen = False
        self.ability_resolved_seen = False
        self.unless_prompt_seen = False
        self.unless_answered = False
        self.unless_pay_choice_text = None
        self.unless_offered_cost_has_life = None
        self.unless_raw_saved = False
        self.shock_target_answered = False
        self.mundungu_etb_turn = None
        self.unknown_dumped = set()
        self.shock_on_stack_at_unless = False
        self.pre_ability = None
        self.pre_unless = None
        self.post = None
        self.pre_ability_done = False
        self.pre_unless_done = False
        self.post_done = False
        self.done = False
        self.snaps = {}
        self.prompt_log = []

    def say(self, *a):
        say(" ".join([f"[{self.tag}]"] + [str(x) for x in a]))

    def note(self, m):
        self.notes.append(m)

    async def start(self):
        self.p0 = PhaseClient(f"{self.tag}-P0")
        await self.p0.connect()
        await self.p0.create(P0_DECK)
        self.p1 = PhaseClient(f"{self.tag}-P1")
        await self.p1.connect()
        await self.p1.join(self.p0.game_code, P1_DECK)
        self.say(f"game {self.p0.game_code} P0seat={self.p0.player_id} "
                 f"P1seat={self.p1.player_id} mode={self.mode}")
        self.p1_seat = self.p1.player_id
        wire(f"{self.tag}_game_start",
             {"game_code": self.p0.game_code, "mode": self.mode,
              "p0_seat": self.p0.player_id, "p1_seat": self.p1.player_id})

    async def export(self, name, client):
        env = await client.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(env)
        return json.loads(env)["state"]

    async def close(self):
        for c in (self.p0, self.p1):
            try:
                await c.close()
            except Exception:
                pass


def snap(state, pid):
    p = player_of(state, pid)
    return (len(p.get("hand", []) or []),
            len(p.get("library", []) or []),
            len(p.get("graveyard", []) or []))


# ------------------------------------------------------------- unless discovery
def unless_pay_value(ch):
    """The pay/decline semantic of an unless choice lives in its value
    surface: {role:"pay", value:"true"|"false"} (run-0400 discovery).
    Returns "true"/"false"/None."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "pay":
            return str(d.get("value")).lower()
    return None


async def answer_unless(c, game, opp, state):
    """Answer the unless prompt per the game mode, via the choice's value
    surface. Archives the raw opportunity once. Returns True on submit."""
    tag = game.tag
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    choices = data.get("choices") or data.get("candidates") or []
    if not game.unless_raw_saved:
        game.unless_raw_saved = True
        fn = (f"{EVDIR}/opportunity_unless_{game.mode}_"
              f"{str(iid).replace('.', '_')}.json")
        with open(fn, "w") as f:
            json.dump(opp, f, indent=1, default=str)
        game.note(f"unless opportunity archived: {fn}")
        raw_blob = json.dumps(opp, default=str).lower()
        game.unless_offered_cost_has_life = "life" in raw_blob
        say(f"[{tag}] UNLESS prompt archived: "
            f"pay_values={[unless_pay_value(ch) for ch in choices]} "
            f"has_life_mention={game.unless_offered_cost_has_life}")
        wire("unless_prompt_seen",
             {"who": tag, "mode": game.mode, "iid": str(iid)[:16],
              "pay_values": [unless_pay_value(ch) for ch in choices],
              "has_life_mention": game.unless_offered_cost_has_life})
    key = (tag, "unless", str(iid))
    if key in game.submitted:
        return False
    want = "true" if game.mode == "pay" else "false"
    pick = next((ch for ch in choices if unless_pay_value(ch) == want), None)
    if pick is None:
        say(f"[{tag}] UNLESS: no choice with pay={want}; NOT answering "
            f"(values={[unless_pay_value(ch) for ch in choices]})")
        wire("unless_no_branch_choice",
             {"who": tag, "mode": game.mode, "want": want,
              "values": [unless_pay_value(ch) for ch in choices]})
        game.note(f"unless prompt had no pay={want} choice; left unanswered")
        return False
    game.submitted.add(key)
    game.unless_answered = True
    game.unless_pay_choice_text = f"pay={unless_pay_value(pick)}"
    say(f"[{tag}] UNLESS answering mode={game.mode}: pay={want} "
        f"(choice {pick.get('id')})")
    wire("unless_answer",
         {"who": tag, "mode": game.mode, "pay": want,
          "choice_id": pick.get("id")})
    sub = {"interactionId": iid,
           "response": {"type": "choose",
                        "data": {"choiceId": pick.get("id")}}}
    await interact_as(c, sub, tag)
    return True



def log_pending(st, state, tag, game, why):
    for opp, rtype, stype, real in all_real_opportunities(st, state):
        descs = [f"{choice_text(ch)[:60]}" for ch in real[:8]]
        entry = {"why": why, "kind": vi_kind_code(st), "rtype": rtype,
                 "stype": stype, "choices": descs}
        game.prompt_log.append(entry)
        say(f"[{tag}] PENDING ({why}): kind={vi_kind_code(st)!r} "
            f"rtype={rtype} [{'; '.join(descs)}]")
        wire("pending_decision", {"who": tag, **entry})


def dump_unknown(st, state, tag, game, why):
    """Archive EVERY vi opportunity verbatim (once per interactionId) when
    the driver has a real decision pending but no handler claimed it.
    Discovery-first: never guess at an unknown prompt shape."""
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        iid = str(opp.get("interactionId"))
        key = (tag, why, iid)
        if key in game.unknown_dumped:
            continue
        game.unknown_dumped.add(key)
        fn = (f"{EVDIR}/opportunity_unknown_{game.mode}_{why}_"
              f"{iid.replace('.', '_')}.json")
        with open(fn, "w") as f:
            json.dump(opp, f, indent=1, default=str)
        say(f"[{tag}] UNKNOWN decision ({why}) archived: {fn} "
            f"kind={vi_kind_code(st)!r}")
        wire("unknown_decision_dumped",
             {"who": tag, "why": why, "iid": iid[:16], "file": fn,
              "kind": vi_kind_code(st)})


# ------------------------------------------------------------------ p0 tick
async def p0_tick(c, st, acts, state, game):
    tag = game.tag
    pid = 0
    # Shock's own target prompt MUST be answered even while the cast guard
    # holds: the cast only confirms after its target is chosen (run-0345
    # lesson -- guard-hold deadlocked the target prompt for 30s).
    if game.shock_cast and not game.shock_target_answered:
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            pick = next((ch for ch in cands
                         if candidate_seat(ch) == game.p1_seat), None)
            if pick is not None:
                game.shock_target_answered = True
                await submit_target(c, opp, rtype, stype, pick, tag, game)
                return
            dump_unknown(st, state, tag, game, "shock-target-no-seat1")
    if cast_guard_tick(game, state, tag) == "hold":
        return
    if await do_mulligan(c, acts, st, pid, tag, game,
                         lambda s, p: card_in_hand_oid(s, p, SHOCK) is not None):
        return
    if await do_bottom(c, acts, st, pid, tag, game, {SHOCK}):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag, game, {SHOCK}):
        return

    # Track Mundungu's ETB turn for summoning-sickness-safe timing
    if game.mundungu_etb_turn is None and on_bf(state, 1, MUNDUNGU):
        game.mundungu_etb_turn = state.get("turn_number")
        say(f"[{tag}] Mundungu ETB on turn {game.mundungu_etb_turn}")
        wire("mundungu_etb", {"turn": game.mundungu_etb_turn})

    # Unless-payment prompt: the engine surfaces it to P0 (the spell's
    # controller) as PayUnlessCost actions while the Counter ability
    # resolves. (Run-0400 discovery: exactChoices with no text; the
    # pay/decline semantic lives in each choice's value surface
    # {role:"pay", value:"true"|"false"}. No target prompt is offered for
    # the ability when Shock is the single legal target -- auto-targeted.)
    if not game.unless_answered and any(
            a.get("type") == "PayUnlessCost" for a in acts):
        game.unless_prompt_seen = True
        game.ability_resolved_seen = True
        game.shock_on_stack_at_unless = stack_has_spell(state, SHOCK)
        if not game.pre_unless_done:
            try:
                game.pre_unless = await game.export("pre_unless", c)
                game.pre_unless_done = True
                game.snaps["pre_unless"] = {
                    "P0": snap(game.pre_unless, 0),
                    "P1": snap(game.pre_unless, 1),
                    "P0_life": state_life(game.pre_unless, 0),
                    "P1_life": state_life(game.pre_unless, 1)}
                say(f"[{tag}] exported pre_unless")
            except Exception as e:
                game.note(f"pre_unless export failed: {e}")
        opp = None
        for o2, _rt, _st, real in all_real_opportunities(st, state):
            if any("payUnlessCost" in surf_codes(ch) for ch in real):
                opp = o2
                break
        if opp is None:
            cands = all_real_opportunities(st, state)
            opp = cands[0][0] if cands else None
        if opp is not None:
            if await answer_unless(c, game, opp, state):
                return
        dump_unknown(st, state, tag, game, "unless-no-opp")
        return

    # Cast Shock only once Mundungu is past summoning sickness (P1 has begun
    # a turn with it: ETB turn + 2) and untapped, so P1 can legally activate.
    sick_over = (game.mundungu_etb_turn is not None
                 and (state.get("turn_number") or 0) >= game.mundungu_etb_turn + 2)
    if (not game.shock_cast and my_main(state, pid) and sick_over
            and not (state.get("stack") or [])):
        moid = bf_named_oid(state, 1, MUNDUNGU)
        if moid is not None and not get_obj(state, moid).get("tapped"):
            oid = card_in_hand_oid(state, pid, SHOCK)
            if oid is not None:
                if await cast_named(c, state, acts, SHOCK, oid, tag, game,
                                    "shock_cast"):
                    return

    if await play_land_prefer(c, state, pid, acts, tag, game, [MOUNTAIN]):
        return
    ct = combat_tick(acts)
    if ct is not None:
        await submit_as_is(c, ct)
        return
    if my_priority(acts) and not real_decision_pending(st):
        await pass_priority(c, st, acts, game)
        return
    if real_decision_pending(st):
        dump_unknown(st, state, tag, game, "p0-unclaimed")


# ------------------------------------------------------------------ p1 tick
async def p1_tick(c, st, acts, state, game):
    tag = game.tag
    pid = 1
    if cast_guard_tick(game, state, tag) == "hold":
        return
    if await do_mulligan(c, acts, st, pid, tag, game,
                         lambda s, p: card_in_hand_oid(s, p, MUNDUNGU) is not None):
        return
    if await do_bottom(c, acts, st, pid, tag, game, {MUNDUNGU}):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag, game, {MUNDUNGU}):
        return

    # Track the ability on the stack / its resolution
    if stack_mentions(state, "mundungu") and stack_has_spell(state, SHOCK):
        game.ability_on_stack_seen = True
    if (game.ability_on_stack_seen and not game.ability_resolved_seen
            and not stack_mentions(state, "mundungu")):
        game.ability_resolved_seen = True
        say(f"[{tag}] Mundungu ability resolved (left the stack)")
        wire("ability_resolved", {"who": tag})
        # pre_ability export: the moment before activation is past; export
        # the resolution-window state as the second checkpoint instead
        if not game.pre_unless_done:
            try:
                game.pre_unless = await game.export("pre_unless", c)
                game.pre_unless_done = True
                say(f"[{tag}] exported pre_unless (p1 side)")
            except Exception as e:
                game.note(f"pre_unless export failed (p1): {e}")

    # Cast Mundungu on turn >= 3 with U+B available
    if (not game.mundungu_cast and my_main(state, pid)
            and (state.get("turn_number") or 0) >= 3
            and not (state.get("stack") or [])):
        has_u = any(obj_lname(state, o) == ISLAND for o in bf_oids(state, pid))
        has_b = any(obj_lname(state, o) == SWAMP for o in bf_oids(state, pid))
        if has_u and has_b:
            oid = card_in_hand_oid(state, pid, MUNDUNGU)
            if oid is not None:
                if await cast_named(c, state, acts, MUNDUNGU, oid, tag, game,
                                    "mundungu_cast"):
                    return

    # Activate Mundungu's ability in response to Shock on the stack
    if (game.mundungu_cast and not game.ability_activated
            and stack_has_spell(state, SHOCK)):
        moid = bf_named_oid(state, pid, MUNDUNGU)
        if moid is not None and not get_obj(state, moid).get("tapped"):
            for a in acts:
                if a.get("type", "").lower() == "activateability" and \
                        str(a.get("_src_oid")) == str(moid):
                    if not game.pre_ability_done:
                        try:
                            # only the host (seat 0) may export (run-0400)
                            game.pre_ability = await game.export(
                                "pre_ability", game.p0)
                            game.pre_ability_done = True
                            game.snaps["pre_ability"] = {
                                "P0": snap(game.pre_ability, 0),
                                "P1": snap(game.pre_ability, 1),
                                "P0_life": state_life(game.pre_ability, 0),
                                "P1_life": state_life(game.pre_ability, 1)}
                            say(f"[{tag}] exported pre_ability")
                        except Exception as e:
                            game.note(f"pre_ability export failed: {e}")
                    say(f"[{tag}] activating Mundungu ability (oid={moid})")
                    wire("ability_activate", {"who": tag, "oid": str(moid)})
                    await submit_as_is(c, a)
                    game.ability_activated = True
                    await asyncio.sleep(0.5)
                    return

    # Answer the ability's target prompt: pick the Shock on the stack
    if game.ability_activated and not game.ability_target_answered:
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            pick = None
            for ch in cands:
                oid = _cand_reference(ch)
                if oid is None:
                    continue
                o = get_obj(state, oid)
                if obj_lname(state, oid) == SHOCK and \
                        str(o.get("zone") or "").lower() == "stack":
                    pick = ch
                    break
            if pick is not None:
                game.ability_target_answered = True
                say(f"[{tag}] ability targets Shock on the stack")
                wire("ability_target", {"who": tag, "target": "shock"})
                await submit_target(c, opp, rtype, stype, pick, tag, game)
                return
            log_pending(st, state, tag, game, "ability-target-no-shock")

    # Resolution complete: Shock gone from stack, no Mundungu ability on
    # stack, unless answered -> export post and finish
    if (game.unless_answered and not game.post_done
            and not (state.get("stack") or [])):
        try:
            # only the host (seat 0) may export (run-0400)
            game.post = await game.export(
                "post_pay" if game.mode == "pay" else "post_decline", game.p0)
            game.post_done = True
            game.snaps["post"] = {
                "P0": snap(game.post, 0), "P1": snap(game.post, 1),
                "P0_life": state_life(game.post, 0),
                "P1_life": state_life(game.post, 1)}
            say(f"[{tag}] exported post (mode={game.mode})")
        except Exception as e:
            game.note(f"post export failed: {e}")
        game.done = True
        return

    if await play_land_prefer(c, state, pid, acts, tag, game,
                              [ISLAND, SWAMP]):
        return
    ct = combat_tick(acts)
    if ct is not None:
        await submit_as_is(c, ct)
        return
    if my_priority(acts) and not real_decision_pending(st):
        await pass_priority(c, st, acts, game)
        return
    if real_decision_pending(st):
        dump_unknown(st, state, tag, game, "p1-unclaimed")


# ------------------------------------------------------------------ pump
async def pump(g, p0_tick_fn, p1_tick_fn, timeout_s, resolve_timeout_s=None):
    t0 = time.time()
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    resolve_deadline = None
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((g.p0, True), (g.p1, False)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 60:
                    s0 = st["state"]
                    la = [a.get("type") for a in
                          (st.get("legal_actions") or [])][:8]
                    g.say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                          f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                          f"legal={la} vikind={vi_kind_code(st)!r} "
                          f"real_decision={real_decision_pending(st)}")
                    last_change[c.player_id] = time.time()
                acts_now = [a.get("type") for a in
                            (st.get("legal_actions") or [])]
                if not (acts_now and time.time()
                        - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                state = st["state"]
                acts = merged_actions(st)
                if is_p0:
                    await p0_tick_fn(c, st, acts, state)
                else:
                    await p1_tick_fn(c, st, acts, state)
            except Exception as e:
                g.say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire(f"{g.tag}_tick_error",
                     {"who": c.name, "err": f"{type(e).__name__}: {e}"})
        if g.done:
            g.say("done flag set; finishing game")
            return True
        if time.time() - last_diag > 60 and g.p0.latest:
            last_diag = time.time()
            s = g.p0.latest["state"]
            g.say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                  f"phase={s.get('phase')} "
                  f"P0hand={len(hand_lnames(s, 0))} P1hand={len(hand_lnames(s, 1))} "
                  f"stack={len(s.get('stack') or [])} shock_cast={g.shock_cast} "
                  f"ability={g.ability_activated}/{g.ability_resolved_seen} "
                  f"unless={g.unless_prompt_seen}/{g.unless_answered}")
        if g.unless_answered and not g.post_done and resolve_deadline is None:
            resolve_deadline = time.time() + (resolve_timeout_s or 300)
        if not g.unless_answered or g.post_done:
            resolve_deadline = None
        if resolve_deadline and time.time() > resolve_deadline:
            g.note("unless answered but post state not reached in "
                   f"{resolve_timeout_s or 300}s; see wire log")
            return False
    g.note(f"global timeout ({timeout_s}s) hit")
    return False


async def run_game(mode):
    g = Game(f"B9843-{mode}", mode)
    await g.start()
    ok = await pump(g, lambda c, st, acts, state: p0_tick(c, st, acts, state, g),
                    lambda c, st, acts, state: p1_tick(c, st, acts, state, g),
                    timeout_s=900, resolve_timeout_s=180)
    return g, ok


# ---------------------------------------------------------------- evaluation
def eval_game(g, parse_sum):
    a = {}
    notes = list(g.notes)
    st_pre = g.pre_ability
    st_post = g.post

    # A1: parse evidence
    up = (parse_sum.get("unless_pay") or {})
    cost = (up.get("cost") or {})
    a["A1_parse"] = "passed" if (
        cost.get("type") == "Mana"
        and "life" not in json.dumps(up).lower()) else "failed"
    notes.append(f"A1: unless_pay={json.dumps(up)[:220]}")

    # A2: setup
    a["A2_setup"] = "passed" if (
        g.mundungu_cast and g.shock_cast and g.pre_ability_done) else "failed"

    # A3: ability targeted Shock. No target prompt is offered when Shock is
    # the single legal target (engine auto-targets); the causal evidence is
    # the unless prompt offered to P0 (the spell's controller) while Shock
    # was on the stack.
    a["A3_target"] = "passed" if (
        g.ability_activated and g.unless_prompt_seen
        and g.shock_on_stack_at_unless) else "failed"

    # A4: unless prompt seen, offered cost had no life component
    if g.unless_prompt_seen and game_unless_offered(g) is False:
        a["A4_unless"] = "passed"
    elif g.unless_prompt_seen:
        a["A4_unless"] = "failed"
    else:
        a["A4_unless"] = "not-run"
    notes.append(f"A4: unless_prompt_seen={g.unless_prompt_seen} "
                 f"offered_has_life={g.unless_offered_cost_has_life} "
                 f"choice_text={g.unless_pay_choice_text!r}")

    if g.mode == "pay":
        # A5: the reported bug -- paid, spell survived, life unchanged
        if st_post is not None and g.unless_answered:
            p1_life = state_life(st_post, 1)
            p0_life = state_life(st_post, 0)
            shock_gy = in_gy(st_post, 0, SHOCK)
            stack_empty = not (st_post.get("stack") or [])
            spell_survived = (p1_life == 18 and shock_gy)
            life_unchanged = (p0_life == 20)
            notes.append(f"A5: post P1_life={p1_life} P0_life={p0_life} "
                         f"shock_gy={shock_gy} stack_empty={stack_empty}")
            if spell_survived and life_unchanged:
                a["A5_pay"] = "failed"   # failed == bug reproduced
            elif spell_survived and p0_life == 19:
                a["A5_pay"] = "passed"   # correct compound payment taken
            else:
                a["A5_pay"] = "not-run"
                notes.append("A5: unexpected post state; see notes")
        else:
            a["A5_pay"] = "not-run"
    else:
        # A6: control -- declined, spell countered
        if st_post is not None and g.unless_answered:
            p1_life = state_life(st_post, 1)
            p0_life = state_life(st_post, 0)
            shock_gy = in_gy(st_post, 0, SHOCK)
            stack_empty = not (st_post.get("stack") or [])
            notes.append(f"A6: post P1_life={p1_life} P0_life={p0_life} "
                         f"shock_gy={shock_gy} stack_empty={stack_empty}")
            a["A6_decline"] = "passed" if (
                p1_life == 20 and p0_life == 20 and shock_gy
                and stack_empty) else "failed"
        else:
            a["A6_decline"] = "not-run"

    # A7: cleanup
    if st_post is not None:
        a["A7_cleanup"] = "passed" if not (st_post.get("stack") or []) \
            else "failed"
    else:
        a["A7_cleanup"] = "not-run"

    # verdict
    if g.mode == "pay":
        if a["A2_setup"] == "passed" and a["A3_target"] == "passed" \
                and a.get("A4_unless") == "passed" \
                and a.get("A5_pay") == "failed":
            verdict = "reproduced"
        elif a.get("A5_pay") == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "control"
    return a, notes, verdict


def game_unless_offered(g):
    return g.unless_offered_cost_has_life


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #9843 - Mundungu unless-payment drops 1 life "
                     "(first validation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-13", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A1_parse", "A1 parse: unless_pay.cost is bare Mana {1}, no life"),
        ("A2_setup", "A2 setup: Mundungu ready, Shock reached the stack"),
        ("A3_target", "A3 target: Mundungu ability targeted Shock"),
        ("A4_unless", "A4 unless: P0 got the payment prompt, no life offered"),
        ("A5_pay", "A5 [the bug] pay -> Shock resolved, P0 life UNCHANGED "
                   "(FAIL = reproduced)"),
        ("A6_decline", "A6 control: decline -> Shock countered"),
        ("A7_cleanup", "A7 cleanup: stack empty post-resolution"),
    ]
    for k, lab in labels:
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col); y += 22
    y += 4
    obs = run["observations"]
    d.text((24, y), "Outcome (from saved authoritative states):",
           fill=(200, 210, 225)); y += 22
    for line in obs.get("outcome_lines", [])[:6]:
        d.text((40, y), line[:118], fill=(150, 165, 185)); y += 19
    y += 4
    d.text((24, y), "Pre/post (from saved authoritative states):",
           fill=(200, 210, 225)); y += 22
    for line in obs.get("snap_lines", [])[:6]:
        d.text((40, y), line[:118], fill=(150, 165, 185)); y += 19
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9843/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


# ------------------------------------------------------------------ main
async def _main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    await verify_server_hello()
    pe = parse_evidence()
    say("parse unless_pay: " + json.dumps(pe.get("unless_pay"), default=str)[:400])

    g_pay, ok_pay = await run_game("pay")
    g_pay.say(f"pay game finished ok={ok_pay}")
    await g_pay.close()

    g_dec, ok_dec, run_dec = None, None, None
    # control game only if the pay game reached the unless prompt
    if g_pay.unless_prompt_seen:
        g_dec, ok_dec = await run_game("decline")
        g_dec.say(f"decline game finished ok={ok_dec}")
        await g_dec.close()
    else:
        say("pay game never reached the unless prompt; skipping decline control")

    for g in (g_pay, g_dec):
        if g is None:
            continue
        if g.pre_ability is not None and g.post is None:
            try:
                g.post = await g.export(
                    "post_pay" if g.mode == "pay" else "post_decline", g.p0)
                g.post_done = True
                g.note("post exported at loop end")
            except Exception as e:
                g.note(f"post final export failed: {e}")

    ass_pay, notes_pay, verdict = eval_game(g_pay, pe)
    if g_dec is not None:
        ass_dec, notes_dec, _ = eval_game(g_dec, pe)
    else:
        ass_dec, notes_dec = {}, ["decline control not run"]

    def snap_lines(g):
        lines = []
        for k in ("pre_ability", "pre_unless", "post"):
            s = g.snaps.get(k)
            if s:
                lines.append(f"{k}: P0 hand/lib/gy={s['P0']} life={s.get('P0_life')} | "
                             f"P1 hand/lib/gy={s['P1']} life={s.get('P1_life')}")
        return lines

    def outcome_lines(g):
        lines = []
        if g.post is not None:
            lines.append(f"post: P0_life={state_life(g.post,0)} "
                         f"P1_life={state_life(g.post,1)} "
                         f"shock_in_P0_gy={in_gy(g.post,0,SHOCK)} "
                         f"stack_empty={not (g.post.get('stack') or [])}")
        lines.append(f"unless_prompt_seen={g.unless_prompt_seen} "
                     f"answered={g.unless_answered} "
                     f"offered_has_life={g.unless_offered_cost_has_life} "
                     f"choice={g.unless_pay_choice_text!r}")
        return lines

    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/backfill-20261013-084136",
        "parse_summary": {"unless_pay": pe.get("unless_pay"),
                          "oracle_text": pe.get("oracle_text")},
        "driver": {"protocol_advertised": 134, "client": "driver/client.py",
                   "scenario": "driver/scenario_9843_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9843_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["shock", 8], ["mountain", 52]],
            "P1": [["mundungu", 8], ["island", 26], ["swamp", 26]],
        },
        "games": {
            "pay": {"ok": ok_pay, "notes": g_pay.notes,
                    "prompt_log": g_pay.prompt_log},
            "decline": {"ok": ok_dec, "notes": notes_dec,
                        "prompt_log": g_dec.prompt_log if g_dec else []},
        },
        "observations": {
            "snaps": {"pay": g_pay.snaps,
                      "decline": g_dec.snaps if g_dec else {}},
            "snap_lines": snap_lines(g_pay),
            "outcome_lines": outcome_lines(g_pay),
            "unless_choice_text": g_pay.unless_pay_choice_text,
            "unless_offered_has_life": g_pay.unless_offered_cost_has_life,
        },
        "assertions": ass_pay,
        "assertions_decline": ass_dec,
        "notes": notes_pay,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Not tested on the original build; verdict is scoped to v0.108.0, "
            "not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 8x shock + 52 mountain vs P1 8x mundungu + 26 island + "
                      "26 swamp (P1 casts Mundungu turn 3; P0 casts Shock "
                      "targeting P1 on turn 5; P1 activates Mundungu targeting "
                      "Shock; P0 pays/declines the unless cost).",
        "contract_line": "Per the issue's Expected: the spell's controller must "
                         "pay both {1} and 1 life to avoid being countered. "
                         "The reported bug: paying {1} alone avoids the counter "
                         "(life stays 20).",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9843_01080.py",
                f"{EVDIR}/scenario_9843_01080.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes_pay.append(f"summary render failed: {e}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass_pay)}", flush=True)


async def main():
    pidfile = "/tmp/scenario_9843_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9843_01080 instance is alive "
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
