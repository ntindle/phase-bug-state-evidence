"""Issue #9844: Fertile Imagination -- the "two ... for each card of the chosen
type" multiplier is dropped.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 driver conventions (verified by scenario_9841/9842/9843_01080.py,
2026-10-13): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, real_decision_pending gating
priority passes, vi candidate references are STRINGS (normalize with str()).

ISSUE (phase-rs/phase#9844, opened 2026-10-12, open,
bug/area:parser/priority:p2-wrong-game-result): Fertile Imagination's oracle
text is "Choose a card type. Target opponent reveals their hand. Create two
1/1 green Saproling creature tokens for each card of the chosen type revealed
this way." The token path replaces the whole count with the for-each rider,
so the fixed multiplier is lost and one Saproling is created per card.
Expected: 2 x (number of cards of the chosen type in the revealed hand).
Actual: 1 x (that number).

DRIVER NOTE (run-0412): the card-type choice is a schema "text" opportunity;
the engine expects {"type":"text","data":{"value":"Creature"}} (value taken
from the candidate's value surface). A choiceIds-style submit fails message
parse and the spell resolves with no chosen type -> zero tokens. The driver
confirms acceptance by the opportunity disappearing, never by assumption.

BEHAVIORAL CONTRACT (PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
  P0: 16x fertile imagination + 44x forest. Casts Fertile Imagination on turn
      4 ({2}{G}{G}). "Target opponent" is P0's only opponent -> auto-targeted
      (no target prompt when there is a single legal target).
  P1: 60x diregraf ghoul (every card is a creature).
  During resolution P0 is prompted "Choose a card type" -> answers Creature.
  P1 reveals their hand; N = number of creature cards in P1's hand at reveal.
  Expected per the issue: P0 gets 2*N Saproling tokens.
  Buggy: P0 gets N Saproling tokens.
  The driver counts N from the pre-resolution authoritative export (no draws
  can intervene between the cast and the reveal) and counts Saproling tokens
  controlled by P0 on the battlefield in the post-resolution export.

Assertions:
  A1_parse    pinned card-data Token effect count is Ref(FilteredTrackedSetSize)
              with no fixed x2 multiplier (parse evidence; supports, does not
              decide).
  A2_setup    FI cast, reached the stack, card-type choice answered Creature,
              spell resolved (post exported).
  A3_reveal   N >= 1 creature cards in P1's revealed hand (from pre export).
  A4_bug      [the reported bug] post-state Saproling tokens == 2*N -> passed
              (not-reproduced); == N -> failed (reproduced); else not-run.
  A5_cleanup  stack empty post-resolution.

Verdict rule: reproduced iff A2 and A3 pass and A4 == failed.
not-reproduced iff A4 == passed. blocked iff setup/choice never completed.

Evidence: evidence/9844/<run-id>/pre_resolve.json, post_resolve.json,
parse_evidence.json, opportunity_*.json, run.json, manifest.sha256,
summary.png, scenario_9844_01080.py, wire_log.jsonl, scenario_run.log
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
RUN_ID = "run-9844-v01080-20261013-0435"
ISSUE = 9844
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FI = "fertile imagination"
FOREST = "forest"
GHOUL = "diregraf ghoul"
P0_DECK = deck((FI, 16), (FOREST, 44))
P1_DECK = deck((GHOUL, 60))

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
               "server on 127.0.0.1:9374 started by earlier run "
               "backfill-20261013-084136, reused per playbook"),
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


def is_creature(o):
    return "Creature" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


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
    """Capture the Token effect lowering for Fertile Imagination from the
    PINNED v0.108.0 card-data.json."""
    e = CARD_DATA[FI]
    abil = (e.get("abilities") or [{}])[0]
    sub = abil.get("sub_ability") or {}
    sub2 = sub.get("sub_ability") or {}
    out = {
        "oracle_text": e.get("oracle_text"),
        "choose_effect": abil.get("effect"),
        "reveal_effect": {k: sub.get("effect", {}).get(k)
                          for k in ("type", "target", "reveal")},
        "token_effect": sub2.get("effect"),
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


async def submit_choice(c, opp, rtype, spec_type, ch, tag, game, why):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    key = (why, str(iid), str(cid))
    if key in game.submitted:
        return None
    game.submitted.add(key)
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting {why}: id={cid} kind={sub['response']['type']} "
        f"({choice_text(ch)[:80]})")
    wire(f"{why}_submission", {"who": tag, "submission": sub,
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
CARD_TYPES = {"artifact", "battle", "creature", "enchantment", "instant",
              "kindred", "land", "planeswalker", "sorcery"}


class Game:
    def __init__(self, tag):
        self.tag = tag
        self.p0 = self.p1 = None
        self.p1_seat = 1
        self.mulls = set()
        self.submitted = set()
        self.land_played_turn = {}
        self.lands_locked = False
        self.pending_cast = None
        self.notes = []
        # scenario progress flags
        self.fi_cast = False
        self.fi_target_answered = False
        self.fi_target_auto = None   # True if no target prompt ever appeared
        self.card_type_prompt_seen = False
        self.card_type_submitted = False
        self.card_type_answered = False
        self.card_type_choice_text = None
        self.card_type_raw_saved = False
        self.fi_resolved_seen = False
        self.unknown_dumped = set()
        self.pre = None
        self.post = None
        self.pre_done = False
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
                 f"P1seat={self.p1.player_id}")
        self.p1_seat = self.p1.player_id
        wire(f"{self.tag}_game_start",
             {"game_code": self.p0.game_code,
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


def count_creatures_in_hand(state, pid):
    return sum(1 for o in hand_ids(state, pid)
               if is_creature(get_obj(state, o)))


def count_saprolings(state, pid):
    n = 0
    names = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and str(o.get("controller", -1)) == str(pid):
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            names.append(nm)
            if "saproling" in nm:
                n += 1
    return n, sorted(set(names))


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
        fn = (f"{EVDIR}/opportunity_unknown_{why}_"
              f"{iid.replace('.', '_')}.json")
        with open(fn, "w") as f:
            json.dump(opp, f, indent=1, default=str)
        say(f"[{tag}] UNKNOWN decision ({why}) archived: {fn} "
            f"kind={vi_kind_code(st)!r}")
        wire("unknown_decision_dumped",
             {"who": tag, "why": why, "iid": iid[:16], "file": fn,
              "kind": vi_kind_code(st)})


def log_pending(st, state, tag, game, why):
    for opp, rtype, stype, real in all_real_opportunities(st, state):
        descs = [f"{choice_text(ch)[:60]}" for ch in real[:8]]
        entry = {"why": why, "kind": vi_kind_code(st), "rtype": rtype,
                 "stype": stype, "choices": descs}
        game.prompt_log.append(entry)
        say(f"[{tag}] PENDING ({why}): kind={vi_kind_code(st)!r} "
            f"rtype={rtype} [{'; '.join(descs)}]")
        wire("pending_decision", {"who": tag, **entry})


# ------------------------------------------------------- card-type choice
async def answer_card_type(c, st, state, game):
    """Answer Fertile Imagination's 'Choose a card type' prompt with
    Creature.

    DISCOVERY (run-0412): the opportunity is a schema response with
    spec.type == "text" (NOT a select-by-choiceId). Each candidate carries a
    value surface {"role": "choice", "index": i, "value": "<CardType>"} and
    the engine expects {"type": "text", "data": {"value": "<CardType>"}}.
    Sending {"type": "text", "data": {"choiceIds": [...]}} fails at message
    parse ("missing field `value`") and the choice is never recorded --
    the spell then resolves with no chosen type and creates ZERO tokens.
    Discovery-first: the opportunity is archived verbatim once; answered-ness
    is confirmed by the opportunity disappearing, never by assumption; a
    rejected submit is retried, not marked done.
    """
    tag = game.tag
    found = False
    for opp, rtype, stype, real in all_real_opportunities(st, state):
        texts = [choice_text(ch).lower() for ch in real]
        if not texts:
            continue
        if not any(t in CARD_TYPES for t in texts):
            continue
        found = True
        iid = opp.get("interactionId")
        if not game.card_type_raw_saved:
            game.card_type_raw_saved = True
            game.card_type_prompt_seen = True
            fn = (f"{EVDIR}/opportunity_cardtype_"
                  f"{str(iid).replace('.', '_')}.json")
            with open(fn, "w") as f:
                json.dump(opp, f, indent=1, default=str)
            game.note(f"card-type opportunity archived: {fn}")
            say(f"[{tag}] CARD-TYPE prompt archived: rtype={rtype} "
                f"stype={stype} choices={texts[:12]}")
            wire("cardtype_prompt_seen",
                 {"who": tag, "iid": str(iid)[:16], "rtype": rtype,
                  "stype": stype, "choices": texts[:12]})
        pick = next((ch for ch in real
                     if choice_text(ch).lower() == "creature"), None)
        if pick is None:
            say(f"[{tag}] CARD-TYPE: no 'Creature' choice; NOT answering")
            wire("cardtype_no_creature_choice",
                 {"who": tag, "choices": texts[:12]})
            game.note("card-type prompt had no Creature choice; left unanswered")
            return True  # claimed (recognized), but not answered
        # value surface carries the engine-expected text
        value = None
        for s in pick.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("role") == "choice" \
                    and d.get("value"):
                value = str(d["value"])
                break
        if value is None:
            value = choice_text(pick)
        key = (tag, "cardtype", str(iid))
        if key not in game.submitted:
            game.submitted.add(key)
            sub = {"interactionId": iid,
                   "response": {"type": "text", "data": {"value": value}}}
            say(f"[{tag}] submitting cardtype text value={value!r}")
            wire("cardtype_submission",
                 {"who": tag, "submission": sub, "choice_text": value})
            nrej = len(c.rejections)
            await interact_as(c, sub, tag)
            await asyncio.sleep(1.0)
            if len(c.rejections) > nrej:
                rej = c.rejections[-1]
                game.note(f"cardtype submit got rejection: "
                          f"{json.dumps(rej)[:200]}; will retry")
                say(f"[{tag}] cardtype submit rejected; will retry next tick")
                wire("cardtype_rejected", {"who": tag,
                                           "rej": str(rej)[:200]})
                game.submitted.discard(key)
            else:
                game.card_type_submitted = True
        return True
    if game.card_type_submitted and not found:
        # the opportunity is gone -> the engine accepted the choice
        game.card_type_answered = True
        game.card_type_choice_text = "Creature"
        say(f"[{tag}] card-type opportunity gone; choice accepted")
        wire("cardtype_accepted", {"who": tag})
    return found


# ------------------------------------------------------------------ p0 tick
async def p0_tick(c, st, acts, state, game):
    tag = game.tag
    pid = 0
    # FI's own target prompt ("target opponent") must be answered even while
    # the cast guard holds: the cast only confirms after its target is chosen
    # (run-0345 lesson). With a single opponent the engine auto-targets and
    # no prompt appears.
    if game.fi_cast and not game.fi_target_answered:
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            pick = next((ch for ch in cands
                         if candidate_seat(ch) == game.p1_seat), None)
            if pick is not None:
                game.fi_target_answered = True
                game.fi_target_auto = False
                await submit_choice(c, opp, rtype, stype, pick, tag, game,
                                    "target")
                return
            dump_unknown(st, state, tag, game, "fi-target-no-seat1")
    if cast_guard_tick(game, state, tag) == "hold":
        return
    if await do_mulligan(c, acts, st, pid, tag, game,
                         lambda s, p: card_in_hand_oid(s, p, FI) is not None):
        return
    if await do_bottom(c, acts, st, pid, tag, game, {FI}):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag, game, {FI}):
        return

    # Card-type choice during resolution
    if game.fi_cast and not game.card_type_answered:
        if await answer_card_type(c, st, state, game):
            return

    # Pre-resolution export: FI on the stack, P1's hand is the reveal set
    # (no draws can intervene between the cast and the reveal)
    if (game.fi_cast and not game.pre_done
            and stack_has_spell(state, FI)):
        try:
            game.pre = await game.export("pre_resolve", c)
            game.pre_done = True
            n = count_creatures_in_hand(game.pre, 1)
            game.snaps["pre_resolve"] = {
                "P0": snap(game.pre, 0), "P1": snap(game.pre, 1),
                "P1_hand_creatures": n,
                "P1_hand_names": sorted(set(hand_lnames(game.pre, 1)))}
            say(f"[{tag}] exported pre_resolve: P1 hand creatures={n}")
        except Exception as e:
            game.note(f"pre_resolve export failed: {e}")

    # Cast FI: turn >= 4, 4+ forests on the battlefield, own main, empty stack
    if (not game.fi_cast and my_main(state, pid)
            and (state.get("turn_number") or 0) >= 4
            and not (state.get("stack") or [])):
        forests = sum(1 for o in bf_oids(state, pid)
                      if obj_lname(state, o) == FOREST)
        if forests >= 4:
            oid = card_in_hand_oid(state, pid, FI)
            if oid is not None:
                if await cast_named(c, state, acts, FI, oid, tag, game,
                                    "fi_cast"):
                    return

    # Resolution complete: choice answered, FI off the stack, stack empty
    if (game.card_type_answered and game.fi_cast and not game.post_done
            and not stack_has_spell(state, FI)
            and not (state.get("stack") or [])):
        game.fi_resolved_seen = True
        try:
            game.post = await game.export("post_resolve", c)
            game.post_done = True
            n, names = count_saprolings(game.post, 0)
            game.snaps["post_resolve"] = {
                "P0": snap(game.post, 0), "P1": snap(game.post, 1),
                "P0_saprolings": n,
                "P0_bf_names": names}
            say(f"[{tag}] exported post_resolve: P0 saprolings={n}")
        except Exception as e:
            game.note(f"post_resolve export failed: {e}")
        game.done = True
        return

    if await play_land_prefer(c, state, pid, acts, tag, game, [FOREST]):
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
                         lambda s, p: True):
        return
    if await do_bottom(c, acts, st, pid, tag, game, set()):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag, game, set()):
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
async def pump(g, timeout_s, resolve_timeout_s=None):
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
                    await p0_tick(c, st, acts, state, g)
                else:
                    await p1_tick(c, st, acts, state, g)
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
                  f"stack={len(s.get('stack') or [])} fi_cast={g.fi_cast} "
                  f"cardtype={g.card_type_prompt_seen}/{g.card_type_answered} "
                  f"resolved={g.fi_resolved_seen}")
        if g.card_type_answered and not g.post_done and resolve_deadline is None:
            resolve_deadline = time.time() + (resolve_timeout_s or 300)
        if not g.card_type_answered or g.post_done:
            resolve_deadline = None
        if resolve_deadline and time.time() > resolve_deadline:
            g.note("card-type answered but post state not reached in "
                   f"{resolve_timeout_s or 300}s; see wire log")
            return False
    g.note(f"global timeout ({timeout_s}s) hit")
    return False


# ---------------------------------------------------------------- evaluation
def eval_game(g, parse_sum):
    a = {}
    notes = list(g.notes)

    # A1: parse evidence -- the Token count lowering dropped the fixed x2
    tok = (parse_sum.get("token_effect") or {})
    cnt = (tok.get("count") or {})
    blob = json.dumps(cnt)
    a["A1_parse"] = "passed" if (
        cnt.get("type") == "Ref" and '"value": 2' not in blob
        and "FilteredTrackedSetSize" in blob) else "failed"
    notes.append(f"A1: token count={blob[:220]}")

    # A2: setup -- FI cast, on stack, card-type answered Creature, resolved
    a["A2_setup"] = "passed" if (
        g.fi_cast and g.pre_done and g.card_type_answered
        and g.card_type_choice_text == "Creature"
        and g.post_done) else "failed"
    notes.append(f"A2: fi_cast={g.fi_cast} pre_done={g.pre_done} "
                 f"cardtype_seen={g.card_type_prompt_seen} "
                 f"answered={g.card_type_answered} "
                 f"choice={g.card_type_choice_text!r} post_done={g.post_done} "
                 f"target_auto={g.fi_target_auto}")

    # A3: N = creature cards in P1's hand at reveal (from pre export)
    n = None
    if g.pre is not None:
        n = count_creatures_in_hand(g.pre, 1)
        a["A3_reveal"] = "passed" if n >= 1 else "failed"
        notes.append(f"A3: P1 hand creatures at reveal N={n}; "
                     f"hand={sorted(set(hand_lnames(g.pre, 1)))}")
    else:
        a["A3_reveal"] = "not-run"

    # A4: THE BUG -- tokens observed vs 2*N expected
    toks = None
    if g.post is not None and n is not None:
        toks, names = count_saprolings(g.post, 0)
        notes.append(f"A4: P0 saprolings={toks} (expected 2*N={2*n}); "
                     f"P0 bf names={names}")
        if toks == 2 * n:
            a["A4_bug"] = "passed"     # correct count -> not-reproduced
        elif toks == n:
            a["A4_bug"] = "failed"     # multiplier dropped -> reproduced
        else:
            a["A4_bug"] = "not-run"
            notes.append(f"A4: unexpected token count {toks}; see notes")
    else:
        a["A4_bug"] = "not-run"

    # A5: cleanup
    if g.post is not None:
        a["A5_cleanup"] = "passed" if not (g.post.get("stack") or []) \
            else "failed"
    else:
        a["A5_cleanup"] = "not-run"

    if (a["A2_setup"] == "passed" and a["A3_reveal"] == "passed"
            and a.get("A4_bug") == "failed"):
        verdict = "reproduced"
    elif a.get("A4_bug") == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, notes, verdict, n, toks


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), 'Issue #9844 - Fertile Imagination "two ... for each card" '
                     'multiplier dropped (first validation)',
           fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-13", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A1_parse", "A1 parse: token count is bare for-each Ref, no x2"),
        ("A2_setup", "A2 setup: FI cast, card-type answered Creature, resolved"),
        ("A3_reveal", "A3 reveal: N creature cards in P1 hand at reveal"),
        ("A4_bug", "A4 [the bug] tokens == 2*N (FAIL=reproduced, got N)"),
        ("A5_cleanup", "A5 cleanup: stack empty post-resolution"),
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9844/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


# ------------------------------------------------------------------ main
async def _main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    await verify_server_hello()
    pe = parse_evidence()
    tok = (pe.get("token_effect") or {})
    say("parse token count: " + json.dumps(tok.get("count"), default=str)[:300])

    g = Game("B9844")
    await g.start()
    ok = await pump(g, timeout_s=900, resolve_timeout_s=180)
    g.say(f"game finished ok={ok}")
    await g.close()

    if g.pre is not None and g.post is None:
        try:
            g.post = await g.export("post_resolve", g.p0)
            g.post_done = True
            g.note("post exported at loop end")
        except Exception as e:
            g.note(f"post final export failed: {e}")

    ass, notes, verdict, n, toks = eval_game(g, pe)

    def snap_lines(g):
        lines = []
        for k in ("pre_resolve", "post_resolve"):
            s = g.snaps.get(k)
            if s:
                extra = ""
                if k == "pre_resolve":
                    extra = f" P1_hand_creatures={s.get('P1_hand_creatures')}"
                else:
                    extra = f" P0_saprolings={s.get('P0_saprolings')}"
                lines.append(f"{k}: P0 hand/lib/gy={s['P0']} | "
                             f"P1 hand/lib/gy={s['P1']}{extra}")
        return lines

    def outcome_lines(g):
        lines = []
        lines.append(f"revealed creatures N={n} -> expected 2*N={2*n if n is not None else '?'} "
                     f"saprolings; observed={toks}")
        lines.append(f"cardtype_prompt_seen={g.card_type_prompt_seen} "
                     f"answered={g.card_type_answered} "
                     f"choice={g.card_type_choice_text!r}")
        if g.post is not None:
            lines.append(f"post: stack_empty={not (g.post.get('stack') or [])} "
                         f"fi_in_P0_gy={'fertile imagination' in [obj_lname(g.post, o) for o in player_of(g.post, 0).get('graveyard', [])]}")
        return lines

    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/backfill-20261013-084136",
        "parse_summary": {"token_count": tok.get("count"),
                          "oracle_text": pe.get("oracle_text")},
        "driver": {"protocol_advertised": 134, "client": "driver/client.py",
                   "scenario": "driver/scenario_9844_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9844_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["fertile imagination", 16], ["forest", 44]],
            "P1": [["diregraf ghoul", 60]],
        },
        "games": {
            "main": {"ok": ok, "notes": g.notes,
                     "prompt_log": g.prompt_log},
        },
        "observations": {
            "snaps": {"main": g.snaps},
            "snap_lines": snap_lines(g),
            "outcome_lines": outcome_lines(g),
            "revealed_creatures_N": n,
            "observed_saprolings": toks,
            "expected_saprolings": 2 * n if n is not None else None,
            "card_type_choice": g.card_type_choice_text,
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "16x/60x deck densities are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Not tested on the original build; verdict is scoped to v0.108.0, "
            "not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 16x fertile imagination + 44 forest vs P1 60x diregraf "
                      "ghoul (P0 casts Fertile Imagination turn 4 targeting P1; "
                      "chooses Creature at the card-type prompt; P1 reveals hand; "
                      "tokens counted in post state).",
        "contract_line": "Per the issue's Expected: create 2 x (number of cards "
                         "of the chosen type in the revealed hand) Saprolings. "
                         "The reported bug: only 1 x that number is created.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9844_01080.py",
                f"{EVDIR}/scenario_9844_01080.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes.append(f"summary render failed: {e}")
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
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


async def main():
    pidfile = "/tmp/scenario_9844_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9844_01080 instance is alive "
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
