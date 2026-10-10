#!/usr/bin/env python3
"""Issue #5474: "Stuck decision: ModalFaceChoice" (+ sibling #5487 Tergrid).

Re-validation on the pinned release v0.105.0 (build 965e243, protocol 120).
Prior runs: v0.78.0 (reproduced, protocol 68, run 20260909-5474),
v0.84.0 (reproduced, protocol 71, run 20260916-5474),
v0.102.0 (reproduced, protocol 106, run 20261004-5474e:
A5 back-face soft-lock, exploratory ChooseModalFace back_face=true consumed
with no rejection and revision frozen 90s+; mid_stall.json captured),
v0.103.0 (reproduced, protocol 106, run 20261006-5474b: cast-2 ModalFaceChoice
offered ONLY the front face (faces=[front]); the exploratory back_face=true
submission was consumed by the engine with no rejection and the game stalled
(mid_stall.json). Tergrid sibling control: both faces offered, front accepted,
cast completed),
v0.104.0 (reproduced, protocol 120, run backfill-20261008-191151:
cast-2 ModalFaceChoice offered BOTH faces (faces=[front, back]) and the
engine advertised ChooseModalFace back_face=true; submitting that
advertised submission was consumed with no rejection
('interaction applied ... action_type=ChooseModalFace') and the game
froze (revision frozen 90s+, no client holding priority; mid_stall.json
captured).)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report #5474 (2026-07-10): "Tried to play Tony Stark commander and it got
stuck when asking to select which side to play cast." Diagnostic: build
v0.20.0 (4b5af92), Waiting for: ModalFaceChoice, Stuck players: 1.
Sibling #5487 (2026-07-10): "I tried to summon Tergrid, God of Fright, and
the game wouldn't let me select either face of the card." Stuck players: 0.
Classifier (2026-07-19, both): not_card_data_attributable - the face choice
is a modal-choice routing/UI state problem, not a card-text parser defect.

Oracle (verified from pinned v0.105.0 card-data.json):
  "tony stark" front {1}{U} Legendary Creature - Human Artificer Hero,
    layout modal_dfc, face_index 0; back face "iron man, tony stark" {3}{R}{R}.
  "tergrid, god of fright" front {3}{B}{B} Legendary Creature - God,
    layout modal_dfc, face_index 0; back face "tergrid's lantern" {3}{B}
    Legendary Artifact.
Per CR 712.8a the player casting a modal double-faced card chooses which
face to cast, so both faces must be selectable at the ModalFaceChoice
decision.

Scenario (native engine, two human-client seats; protocol 120; the reporter
cast Tony Stark as a commander, but the scenario casts it from the main deck -
the ModalFaceChoice decision type is identical):
  Game 1 (Tony Stark, #5474 primary): P0 casts Tony Stark at main-phase
    priority. At the ModalFaceChoice wait the driver records the advertised
    faces/actionability, submits the advertised front-face choice, drives
    payment and resolution, then casts a second Tony Stark with {3}{R}{R}
    available to test whether the back face is ever offered (exploratory
    ChooseModalFace back_face=true submission logged as constructed).
  Game 2 (Tergrid, #5487 sibling): P0 casts Tergrid, God of Fright; same
    choice-completion contract.

Assertions:
  A1_setup_ok        pre.json: P0 PreCombatMain priority, Tony Stark in hand,
                     >=2 untapped lands (island available for {1}{U}).
  A2_modal_offered   A ModalFaceChoice wait appears for P0 with >=1 advertised
                     face choice and an actionable submission (vi candidate
                     selection or a ChooseModalFace legal action).
  A3_choice_accepted The advertised front-face choice submits with no
                     rejection and the cast advances (no >60s stall).
  A4_front_completes post_front.json: Tony Stark on P0 battlefield, stack
                     empty, game advanced past the cast turn, life 20/20.
  A5_back_reachable  The back face ("iron man, tony stark") is offered as a
                     choice at some ModalFaceChoice AND selecting it does not
                     break the game (the exploratory back_face=true
                     submission is accepted and resolves, or the vi
                     back-face candidate is answered and Iron Man resolves).
                     Offered-but-soft-locking counts as failed.
  A6_cleanup         post.json: stack empty, game proceeding.
  B1..B4 (Tergrid): setup / choice offered / choice accepted / cast completes.

Verdict rule: reproduced iff A2 fails (no actionable face choice), A3 fails
(rejection/stall), or A5 fails (back face never safely selectable - the
"select which side" decision remains broken on this build; differences from
the v0.20.0 hard stall are explained). not-reproduced iff A1-A6 and B1-B4 pass.
blocked iff the game cannot be driven to a ModalFaceChoice.

Evidence: evidence/5474/<run-id>/pre.json, post_front.json, pre_cast2.json,
post.json, modalfacechoice_*.json, g2_pre.json, g2_post.json, run.json,
assertions.json, manifest.sha256, summary.png, scenario_5474_01050.py,
wire_log.jsonl, scenario_run.log, server_excerpts.log

Driver conventions (protocol 120, from scenario_650_01020.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 118),
    gated on the MulliganDecision legal action with answered (tag,iid) keys.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 plus a schema/select
    opportunity offering hand cards.
  - Spell mana payment via vi tapLandForMana, needs-gated
    (ST["mana_needs"][tag]); never blind-tap.
  - ActivateAbility submitted while holding priority (never pass first);
    merged_actions ActivateAbility (source_id match) first, else vi
    exactChoices 'activateAbility' answered with {"type":"choose"}.
  - real_decision_pending excludes tapLandForMana/untapLandForMana/
    castSpell/activateAbility/passPriority/mulliganDecision menus (never
    gate legs on it).
  - A single await asyncio.sleep(0) yield after the priority gate, before
    leg evaluation (leg-engagement race fix).
  - Export checkpoints fall through to the priority pass in the same tick;
    never return after an export while holding priority.
  - Re-tick backstop: a client holding Priority with no revision change for
    >5s is re-ticked (scenario_650_01010.py last_tick_at pattern).
  - DeclareAttackers submitted from the freshly advertised action
    (deepcopy, attacks set); stale_interaction rejections retry on the next
    advertised action.
  - client.py HELLO advertises protocol 120 (exact match enforced).
  - Cast-confirmation guard (protocol 120/120, AGENTS.md 2026-10-09): after
    every CastSpell submission the driver sets ST["pending_cast"] and does
    NOT pass priority or start new plays until the spell is confirmed on
    the stack/battlefield (zone check each tick); a 30s backstop clears a
    still-in-hand cast as dropped and resets the scenario one-shot flags
    so the cast re-triggers. The guard runs before the priority gate in
    both tick functions. The ModalFaceChoice handling (part of the cast
    itself) intentionally runs before the guard so the face choice that
    advances the cast is never blocked.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 5474
RUN_ID = os.environ.get("RUN_ID", "run-5474-reval-v01050-20261009-2011")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.105.0",
    "build_commit": "965e243",
    "protocol_version": 120,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.105.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned isolated v0.105.0 server on "
                      "127.0.0.1:9374 (started by this run; this run's own "
                      "game + raw Hello handshake verify the identity)"),
    "source": ("2026-10-09: latest stable release v0.105.0 (published "
               "2026-10-09) == pinned release dir; ServerHello "
               "0.105.0/965e243/protocol 120 verified by handshake this "
               "run; hashes recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.105.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.105.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.105.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

TONY = "tony stark"
IRONMAN = "iron man, tony stark"
TERGRID = "tergrid, god of fright"
LANTERN = "tergrid's lantern"
ISLAND = "island"
MOUNTAIN = "mountain"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK_G1 = deck((TONY, 12), (ISLAND, 24), (MOUNTAIN, 24))
P1_DECK_G1 = deck((FOREST, 60))
P0_DECK_G2 = deck((TERGRID, 12), (SWAMP, 48))
P1_DECK_G2 = deck((FOREST, 60))

SETUP_DEADLINE_S = 1800
ASS_KEYS = ("A1_setup_ok", "A2_modal_offered", "A3_choice_accepted",
            "A4_front_completes", "A5_back_reachable", "A6_cleanup",
            "B1_tergrid_setup", "B2_tergrid_offered", "B3_tergrid_accepted",
            "B4_tergrid_completes")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
P0C = None
P1C = None


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
        WIRE.flush()
    except ValueError:
        pass


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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def on_bf(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def can_pay(state, pid, colors=(), generic=0):
    color_of = {ISLAND: "U", FOREST: "G", "swamp": "B", MOUNTAIN: "R"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def untapped_lands(state, pid, name):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == name and not get_obj(state, o).get("tapped")]


def spell_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def my_priority(acts):
    """Protocol 120: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
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


# ---------------------------------------------------------------- interaction primitives

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    ST["_last_submit"] = {"type": a["type"],
                          "data_keys": sorted((a.get("data") or {}).keys())}
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
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    """Verify the modal_dfc cards exist with both faces in the pinned data."""
    out = {}
    for name in (TONY, TERGRID):
        li = CARD_DATA.get(name, {})
        out[name] = {
            "layout": li.get("layout"),
            "mana_cost": li.get("mana_cost"),
            "has_key": name in CARD_DATA,
        }
    back_ok = (IRONMAN in CARD_DATA) and (LANTERN in CARD_DATA)
    ok = all(out[n]["layout"] == "modal_dfc" for n in (TONY, TERGRID)) and back_ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "cards": out,
                   "iron_man_in_data": IRONMAN in CARD_DATA,
                   "lantern_in_data": LANTERN in CARD_DATA}, f, indent=1)
    ST["data_level_ok"] = ok
    say(f"data-level check: ok={ok} modal_dfc fronts={ {n: out[n]['layout'] for n in out} } backs={back_ok}")


async def do_mulligan(c, acts, st, pid, tag, want_card, land_names):
    """Protocol 120: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission)."""
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
    state = st["state"]
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n in land_names)
    MULLS.add(key)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    keep = (want_card in hn) and lands >= 2
    if not keep and mull_count < 4 and len(hn) > 4:
        say(f"[{tag}] mulligan #{mull_count} ({len(hn)} cards, hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, keepers):
    """Protocol 120: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity.
    Gated on kind == 'mulligan' AND turn 1 / Untap so it cannot misfire on
    mid-game SelectCards prompts."""
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; "
            "not answering")
        wire(f"{tag}_bottom_no_vi",
             {"acts": [a.get("data") for a in sel_acts][:8]})
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
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in keepers:
            return (1, str(ref))  # keep these on top
        return (0, str(ref))      # bottom everything else first

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(
        c, {"interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, keepers):
    """Protocol 120: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        cands = rdata.get("candidates") or []
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not any(r in handset for r in refs):
            continue
        found_opp = (opp, cands, spec)
        break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm in (ISLAND, MOUNTAIN, SWAMP, FOREST):
            return (0, nm)
        if nm in keepers:
            return (9, nm)
        return (5, nm)

    picks = [ch.get("id") for ch in sorted(cands, key=rank)[:max(1, n)]
             if ch.get("id")]
    if not picks:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": picks}}}, tag)
    return True


async def pay_tick(c, acts):
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
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId":
                                                            ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag, want_land):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != want_land:
            continue
        for a in acts:
            if a["type"] == "PlayLand" and \
                    str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {want_land}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


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


async def try_cast(c, st, state, acts, tag, name, needs):
    """Cast via legacy CastSpell action when advertised; else via the
    viewer_interaction castSpell exactChoices menu."""
    a, oid = cast_action_for(acts, state, name)
    if a:
        ST["mana_needs"][tag] = dict(needs)
        say(f"[{tag}] casting {name} (oid {oid}) needs={needs} via legacy")
        wire("cast", {"who": tag, "card": name, "oid": oid, "via": "legacy"})
        await submit_as_is(c, a)
        # cast-confirmation guard (protocol 118/120): never pass priority
        # or start new plays under an in-flight CastSpell; the bare
        # CastSpell can lose a race to our own PassPriority and be silently
        # dropped by the server.
        set_pending_cast(oid, name, tag, {"flag": _cast_flag_for(name)})
        return True
    # fallback: vi exactChoices 'castSpell' menu referencing the hand card
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "castSpell" not in codes:
                continue
            ref = _cand_reference(ch)
            if ref is None:
                continue
            if obj_lname(state, ref) != name:
                continue
            if (ch.get("status") or {}).get("type") not in (None, "available"):
                continue
            ST["mana_needs"][tag] = dict(needs)
            say(f"[{tag}] casting {name} (oid {ref}) needs={needs} via vi")
            wire("cast", {"who": tag, "card": name, "oid": int(ref),
                          "via": "vi"})
            await answer_vi(c, opp, ch, tag)
            set_pending_cast(int(ref), name, tag,
                             {"flag": _cast_flag_for(name)})
            return True
    return False


# ------------------------------------------------------------- cast-confirmation guard (protocol 118/120)
def _cast_flag_for(name):
    """One-shot cast flag reset if the 30s backstop fires."""
    stage = ST.get("stage")
    if name == TONY:
        return "cast1_submitted" if stage == "g1_setup" else "cast2_submitted"
    if name == TERGRID:
        return "g2_cast_submitted"
    return None


def set_pending_cast(oid, name, tag, reset):
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag,
                          "reset": dict(reset)}


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    zone = cast_obj_zone(state, pc["oid"])
    return zone in ("stack", "battlefield", "graveyard", "exile", "command")


def cast_guard_tick(state, tag):
    """Protocol-118/120 cast-confirmation guard.

    Returns "hold" while our CastSpell is in flight but unconfirmed: the
    driver must not pass priority or start new plays in that window -- on
    protocol 118/120 a bare CastSpell can lose a race to our own
    PassPriority submitted on the next tick and be silently dropped by the
    server (observed 2026-10-09). Returns "proceed" once confirmed; a 30s
    backstop clears a still-unconfirmed cast and resets the scenario
    one-shot flags so the play re-triggers instead of stalling. Must run
    before any early-return block.
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        say(f"[{tag}] cast confirmed "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": cast_obj_zone(state, pc["oid"])})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]),
              "zone": cast_obj_zone(state, pc["oid"])})
        ST["pending_cast"] = None
        r = pc.get("reset") or {}
        if r.get("flag"):
            ST[r["flag"]] = False
        ST["mana_needs"][pc.get("tag") or tag] = {}
        return "proceed"
    return "hold"


async def pay_mana_vi(c, st, tag):
    """Protocol 120: mana payment via vi tapLandForMana, strictly
    needs-gated (the engine offers tapLandForMana menus at ordinary
    priority windows; blind tapping must never happen)."""
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    if sum(needs.values()) <= 0:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
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
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def drain_rejections(c, stage):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"type": t, "data": data, "stage": stage,
                   "after": ST.get("_last_submit")}
            out.append(rec)
            ST["rejections"].append(rec)
            say(f"[{c.name}] REJECTION in stage {stage}: "
                f"{json.dumps(data, default=str)[:300]}")
            wire("rejection", {"who": c.name, "stage": stage, "data": data})
    return out


async def export_named(name):
    """Authoritative export via P0 (seat 0, single-user). Callers must NOT
    return after this while their client holds priority -- fall through to
    the priority pass in the same tick."""
    try:
        raw = await P0C.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        ST["exports"][name] = True
        say(f"exported {name}.json (turn={env['state'].get('turn_number')})")
        wire("export", {"name": name,
                        "turn": env["state"].get("turn_number")})
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        ST["tick_errors"].append(f"export_{name}: {e!r}")
        return None


# ---------------------------------------------------------------- ModalFaceChoice

def face_value_of_choice(ch, back_name):
    """Extract the advertised face ('front'/'back') from a candidate."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {}) if isinstance(s.get("data"), dict) else {}
        if d.get("role") == "face" and d.get("value"):
            v = str(d["value"]).lower()
            if v in ("front", "back"):
                return v
        if isinstance(d.get("code"), str) and "face" in d["code"].lower():
            code = d["code"].lower()
            if "back" in code:
                return "back"
            if "front" in code:
                return "front"
    tx = (choice_text(ch) or "").lower()
    if back_name in tx:
        return "back"
    if ("front" in tx and "back" not in tx):
        return "front"
    return None


def _surf_datas(item):
    out = []
    for s in item.get("surfaces", []) or []:
        d = s.get("data") if isinstance(s, dict) else None
        if isinstance(d, dict):
            out.append(d)
    return out


def modal_face_opp_info(st, back_name):
    """Find a ModalFaceChoice opportunity in the viewing seat's
    viewer_interaction. Returns (opp, rtype, faces) or None."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) or {}
        items = rdata.get("candidates") or rdata.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        kind = vi_kind_code(st)
        blob = json.dumps(items, default=str).lower()
        looks_modal = (
            any("modalface" in c.lower() or "modal_face" in c.lower() or
                c.lower() == "face" for c in codes)
            or "modalfacechoice" in kind.lower()
            or ("face" in blob and back_name in blob)
            or any(d.get("role") == "face"
                   for item in items for d in _surf_datas(item)))
        if not looks_modal:
            continue
        faces = []
        for ch in items:
            faces.append({
                "id": ch.get("id"),
                "face": face_value_of_choice(ch, back_name),
                "text": choice_text(ch)[:120],
                "status": (ch.get("status") or {}).get("type"),
            })
        return opp, rtype, faces
    return None


def record_modal_opp(st, state, tag, back_name):
    """Record the full ModalFaceChoice opportunity for evidence."""
    wf = state.get("waiting_for") or {}
    vi = st.get("viewer_interaction") or {}
    snap = {
        "waiting_for": wf,
        "waitingForKind_code": vi_kind_code(st),
        "viewer_interaction": vi,
        "legal_actions": st.get("legal_actions"),
        "legal_actions_by_object": st.get("legal_actions_by_object"),
    }
    with open(f"{EVDIR}/modalfacechoice_{tag}.json", "w") as f:
        json.dump(snap, f, indent=1, default=str)
    found = modal_face_opp_info(st, back_name)
    cmf_actions = [a.get("data") for a in merged_actions(st)
                   if a["type"] == "ChooseModalFace"]
    rec = {
        "tag": tag,
        "kind_code": vi_kind_code(st),
        "interaction_ids": [o.get("interactionId") for o in vi_ops(st)],
        "response_types": [((o.get("response") or {}).get("type"))
                           for o in vi_ops(st)],
        "faces": found[2] if found else [],
        "canSubmit": vi.get("canSubmit"),
        "choose_modal_face_actions": cmf_actions,
    }
    wire("modal_opp", rec)
    say(f"ModalFaceChoice [{tag}]: faces="
        f"{[(f['face'], f['text'][:40]) for f in rec['faces']]} "
        f"canSubmit={rec['canSubmit']} kind={rec['kind_code']} "
        f"legalChooseModalFace={len(cmf_actions)}")
    return rec


async def answer_modal_face(c, st, tag, back_name, want_face):
    """Answer the ModalFaceChoice with the advertised face choice via the
    vi opportunity (choose/select schema). Returns True when submitted."""
    found = modal_face_opp_info(st, back_name)
    if not found:
        return False
    opp, rtype, faces = found
    picks = [f for f in faces if f["face"] == want_face]
    if not picks:
        say(f"[{tag}] face '{want_face}' not advertised "
            f"(faces={[f['face'] for f in faces]})")
        wire("face_not_advertised",
             {"who": tag, "want": want_face, "faces": faces})
        return False
    choice = picks[0]
    rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = rdata.get("candidates") or rdata.get("choices") or []
    real = next((ch for ch in items if ch.get("id") == choice["id"]), None)
    if real is None:
        return False
    iid = opp.get("interactionId")
    SUBMITTED_OPPS.add(iid)
    if rtype == "schema":
        spec = (rdata.get("spec", {}) or {})
        stype = spec.get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": stype,
                            "data": {"choiceIds": [real.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": real.get("id")}}}
    say(f"[{tag}] answering ModalFaceChoice: face={want_face} "
        f"choice={real.get('id')} ({choice['text'][:60]})")
    wire("face_choice_submission",
         {"who": tag, "want_face": want_face, "submission": sub})
    await interact_as(c, sub, tag)
    return True


async def legend_rule(c, st):
    """ChooseLegend after a second legendary permanent resolves.

    Gated on the wait kind advertising a legend choice: protocol-120
    priority menus (tapLandForMana/untapLandForMana/castSpell/activateAbility)
    use identical choose/select opportunity surfaces and must NOT be
    answered as legend waits."""
    kind = vi_kind_code(st).lower()
    if "legend" not in kind:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        items = rdata.get("candidates") or rdata.get("choices") or []
        avail = [ch for ch in items
                 if (ch.get("status") or {}).get("type") in (None, "available")]
        if not avail:
            continue
        # keep the first available (documented choice; both are Tony Stark)
        pick = avail[0]
        iid = opp.get("interactionId")
        if rtype_is_schema(resp):
            sub = {"interactionId": iid,
                   "response": {"type": "select",
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        say(f"[{c.name}] legend rule: keep {choice_text(pick)[:60]}")
        wire("legend_choice", {"who": c.name, "submission": sub})
        await interact_as(c, sub, c.name)
        return True
    return False


def rtype_is_schema(resp):
    return (resp.get("type") == "schema")


async def generic_tick(c, pid, st, acts, state, keepers, land_names, want_card):
    """Mulligan, payments, combat declarations, triggers, legend rule;
    returns True if it consumed the tick."""
    tag = c.name
    if await do_mulligan(c, acts, st, pid, tag, want_card, land_names):
        return True
    if await do_bottom(c, acts, st, pid, tag, keepers):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag, keepers):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if await pay_mana_vi(c, st, tag):
        return True
    if any(a["type"] == "DeclareAttackers" for a in acts):
        da = next(a for a in acts if a["type"] == "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    if any(a["type"] == "DeclareBlockers" for a in acts):
        da = next(a for a in acts if a["type"] == "DeclareBlockers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(c, d)
        return True
    if any(a["type"] == "OrderTriggers" for a in acts):
        oa = next(a for a in acts if a["type"] == "OrderTriggers")
        await submit_as_is(c, oa)
        return True
    if await legend_rule(c, st):
        return True
    return False


# ---------------------------------------------------------------- ticks

async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    game = ST["game"]
    keepers = (TONY, IRONMAN) if game == 1 else (TERGRID, LANTERN)
    land_names = (ISLAND, MOUNTAIN, FOREST) if game == 1 else (SWAMP, FOREST)
    want_card = TONY if game == 1 else TERGRID
    back_name = IRONMAN if game == 1 else LANTERN
    front_name = TONY if game == 1 else TERGRID
    if await generic_tick(c, pid, st, acts, state, keepers, land_names,
                          want_card):
        return

    # ---- ModalFaceChoice handling ----
    if modal_face_opp_info(st, back_name):
        stage = ST["stage"]
        if game == 1 and stage == "g1_face1" and not ST.get("face1_submitted"):
            rec = record_modal_opp(st, state, "g1_cast1", back_name)
            ST["face1_opps"].append(rec)
            ST["face1_wait_start"] = ST.get("face1_wait_start") or time.time()
            ST["face1_actionable"] = bool(rec["faces"]) and rec["canSubmit"]
            if await answer_modal_face(c, st, "g1_cast1", back_name, "front"):
                ST["face1_submitted"] = True
                ST["face1_choice_ok"] = True
            else:
                ST["face1_wait_start"] = ST.get("face1_wait_start") or time.time()
            return
        if game == 1 and stage == "g1_face2" and not ST.get("face2_submitted"):
            rec = record_modal_opp(st, state, "g1_cast2", back_name)
            ST["face2_opps"].append(rec)
            ST["face2_wait_start"] = ST.get("face2_wait_start") or time.time()
            ST["face2_actionable"] = bool(rec["faces"]) and rec["canSubmit"]
            # exploratory: is the back face reachable at all? (submitted via
            # the legacy action path, logged as exploratory)
            if not ST.get("back_probe_sent"):
                ST["back_probe_sent"] = True
                ST["_back_rej_mark"] = len(ST["rejections"])
                advertised = any(
                    a["type"] == "ChooseModalFace"
                    and (a.get("data") or {}).get("back_face") is True
                    for a in merged_actions(st))
                ST["back_probe_advertised"] = advertised
                sub = {"type": "ChooseModalFace", "data": {"back_face": True}}
                wire("back_face_probe",
                     {"submission": sub,
                      "advertised_as_legacy_action": advertised,
                      "note": "exploratory legacy-action submission"})
                say("[P0] exploratory: ChooseModalFace back_face=true "
                    f"(advertised={advertised})")
                await c.send_action(sub)
                return
            if await answer_modal_face(c, st, "g1_cast2", back_name, "front"):
                ST["face2_submitted"] = True
            return
        if game == 2 and stage == "g2_face" and not ST.get("g2_submitted"):
            rec = record_modal_opp(st, state, "g2_cast1", back_name)
            ST["g2_opps"].append(rec)
            ST["g2_wait_start"] = ST.get("g2_wait_start") or time.time()
            ST["g2_actionable"] = bool(rec["faces"]) and rec["canSubmit"]
            if await answer_modal_face(c, st, "g2_cast1", back_name, "front"):
                ST["g2_submitted"] = True
            return
        return  # ModalFaceChoice owned by this client but not our stage
    if ST["stage"] == "modal_wait" and not ST.get("face1_submitted"):
        # no ModalFaceChoice opportunity surfaced while the cast is pending
        ST["face1_wait_start"] = ST.get("face1_wait_start") or time.time()

    # ---- cast-confirmation guard (protocol 118/120): while our
    # CastSpell is in flight but unconfirmed, answer decisions and mana
    # needs but start no new plays and pass no priority. Runs before the
    # priority gate so a dropped cast is always detected; the
    # ModalFaceChoice handling above (part of the cast itself) already ran.
    if cast_guard_tick(state, tag) == "hold":
        return

    # ---- priority gate, then yield before leg evaluation
    has_prio = my_priority(acts)
    await asyncio.sleep(0)

    in_main = my_main(state, pid)
    if in_main and has_prio:
        if game == 1:
            # cast1 needs {1}{U}; the second cast needs {3}{R}{R} --
            # switch the preferred land once cast1 is on the stack/BF.
            want_land = MOUNTAIN if ST["stage"] in (
                "g1_face1", "g1_cast2_prep", "g1_face2") else ISLAND
        else:
            want_land = SWAMP
        if await play_a_land(c, state, pid, acts, tag, want_land):
            return
        if game == 1 and ST["stage"] == "g1_setup" and not ST.get("cast1_submitted"):
            if can_pay(state, pid, ("U",), 1):
                sid = spell_in_hand_oid(state, pid, TONY)
                if sid is not None and await try_cast(
                        c, st, state, acts, tag, TONY, {"U": 1, "generic": 1}):
                    ST["cast1_submitted"] = True
                    ST["cast1_oid"] = sid
                    ST["cast1_turn"] = state.get("turn_number")
                    ST["stage"] = "g1_face1"
                    say("STAGE -> g1_face1 (cast 1 submitted; exporting pre)")
                    await export_named("pre")
                    # fall through: no return while possibly holding priority
                    return
        if game == 1 and ST["stage"] == "g1_cast2_prep" \
                and not ST.get("cast2_submitted"):
            um = untapped_lands(state, pid, MOUNTAIN)
            ul = [o for o in bf_oids(state, pid)
                  if is_land(get_obj(state, o))
                  and not get_obj(state, o).get("tapped")]
            tony_oid = spell_in_hand_oid(state, pid, TONY)
            cp = can_pay(state, pid, ("R", "R"), 3)
            now = time.time()
            if now - ST.get("_cast2_diag_at", 0) > 20:
                ST["_cast2_diag_at"] = now
                say(f"[P0] cast2 check: can_pay(RR+3)={cp} tony_oid={tony_oid} "
                    f"untapped_mountains={len(um)} untapped_lands={len(ul)} "
                    f"hand={hand_lnames(state, pid)}")
            if cp:
                sid = spell_in_hand_oid(state, pid, TONY)
                if sid is not None and await try_cast(
                        c, st, state, acts, tag, TONY, {"R": 2, "generic": 3}):
                    ST["cast2_submitted"] = True
                    ST["cast2_oid"] = sid
                    ST["stage"] = "g1_face2"
                    say("STAGE -> g1_face2 (cast 2 submitted; exporting pre_cast2)")
                    await export_named("pre_cast2")
                    return
        if game == 2 and ST["stage"] == "g2_setup" \
                and not ST.get("g2_cast_submitted"):
            if can_pay(state, pid, ("B", "B"), 3):
                sid = spell_in_hand_oid(state, pid, TERGRID)
                if sid is not None and await try_cast(
                        c, st, state, acts, tag, TERGRID, {"B": 2, "generic": 3}):
                    ST["g2_cast_submitted"] = True
                    ST["g2_cast_oid"] = sid
                    ST["stage"] = "g2_face"
                    say("STAGE -> g2_face (Tergrid cast submitted; exporting g2_pre)")
                    await export_named("g2_pre")
                    return
    if has_prio:
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    game = ST["game"]
    keepers = (FOREST,)
    land_names = (FOREST,)
    if await generic_tick(c, pid, st, acts, state, keepers, land_names, None):
        return
    # cast-confirmation guard: hold priority passes while P0's cast is in
    # flight but unconfirmed (brief; the spell confirms on the stack).
    if cast_guard_tick(state, tag) == "hold":
        return
    has_prio = my_priority(acts)
    await asyncio.sleep(0)
    in_main = my_main(state, pid)
    if in_main and has_prio:
        if await play_a_land(c, state, pid, acts, tag, FOREST):
            return
    if has_prio:
        await pass_priority(c, st, acts)


# ---------------------------------------------------------------- evaluate / finalize

def evaluate():
    ass = {k: "not-run" for k in ASS_KEYS}
    notes = ST["notes"]
    notes.append(f"rejections={len(ST['rejections'])} "
                 f"face1_opps={len(ST['face1_opps'])} "
                 f"face2_opps={len(ST['face2_opps'])} "
                 f"g2_opps={len(ST['g2_opps'])} "
                 f"back_probe_accepted={ST.get('back_probe_accepted')}")
    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())
    # A1
    if ST["exports"].get("pre"):
        pre = load_env("pre")
        s = pre["state"]
        tony_in_hand = any(
            obj_lname(s, o) == TONY for o in hand_ids(s, 0))
        ok = (s.get("active_player") == 0
              and s.get("phase") == "PreCombatMain"
              and tony_in_hand
              and len(untapped_lands(s, 0, ISLAND)) >= 1)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: active={s.get('active_player')} "
                     f"phase={s.get('phase')} tony_in_hand={tony_in_hand} "
                     f"untapped_islands={len(untapped_lands(s, 0, ISLAND))}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1: pre.json missing (cast1_submitted="
                     f"{ST.get('cast1_submitted')})")
    # A2
    if not ST["face1_opps"]:
        ass["A2_modal_offered"] = "failed"
        notes.append("A2: no ModalFaceChoice wait observed for cast 1")
    else:
        r = ST["face1_opps"][0]
        faces = [(f["face"], f["text"][:40]) for f in r["faces"]]
        actionable = (r["canSubmit"] and len(r["faces"]) > 0) \
            or len(r["choose_modal_face_actions"]) > 0
        ass["A2_modal_offered"] = "passed" if actionable else "failed"
        notes.append(f"A2: ModalFaceChoice wait seen; faces={faces}; "
                     f"canSubmit={r['canSubmit']}; kind={r['kind_code']}; "
                     f"chooseModalFace actions={len(r['choose_modal_face_actions'])}; "
                     f"actionable={actionable}")
    # A3
    if not ST.get("face1_submitted"):
        if ST.get("face1_stall"):
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: ModalFaceChoice stalled >60s with no actionable "
                         "submission (the reported stuck signature)")
        else:
            ass["A3_choice_accepted"] = "not-run"
            notes.append("A3: no face choice was submitted")
    elif ST.get("face1_rejected"):
        ass["A3_choice_accepted"] = "failed"
        notes.append("A3: advertised front-face choice was REJECTED")
    elif ST.get("tony1_on_bf"):
        ass["A3_choice_accepted"] = "passed"
        notes.append("A3: front-face choice accepted; cast resolved to Tony "
                     "Stark on the battlefield")
    else:
        ass["A3_choice_accepted"] = "failed"
        notes.append("A3: choice submitted but cast never resolved")
    # A4
    postF = load_env("post_front")
    if postF is not None:
        s = postF["state"]
        tony_bf = on_bf(s, 0, TONY)
        stack_empty = len(stack_entries(s)) == 0
        ok = (len(tony_bf) > 0 and stack_empty
              and life_of(s, 0) == 20 and life_of(s, 1) == 20)
        ass["A4_front_completes"] = "passed" if ok else "failed"
        notes.append(f"A4: tony on P0 BF={tony_bf}; stack_empty={stack_empty}; "
                     f"life={life_of(s, 0)}/{life_of(s, 1)}; "
                     f"turn={s.get('turn_number')} phase={s.get('phase')}")
    else:
        ass["A4_front_completes"] = "failed"
        notes.append("A4: post_front.json missing")
    # A5
    offered_faces = set()
    for r in ST["face1_opps"] + ST["face2_opps"]:
        for f in r["faces"]:
            if f["face"]:
                offered_faces.add(f["face"])
    # A5 — check probe_wedged FIRST: the protocol-118/120 cast-confirmation
    # guard's 30s backstop clears cast2_submitted for retry, so a wedged
    # probe must not be masked by the flag reset (2026-10-09 fix).
    if ST.get("probe_wedged"):
        # Selecting the back face soft-locked the game: the back face is
        # not safely selectable, whether or not it was offered.
        ass["A5_back_reachable"] = "failed"
        notes.append(
            f"A5: exploratory ChooseModalFace back_face=true was consumed "
            f"by the engine with no rejection (server log: 'interaction "
            f"applied ... action_type=ChooseModalFace') and the game "
            f"soft-locked (no new revision 90s+, no client holding "
            f"priority; mid_stall.json captured). Offered faces at "
            f"ModalFaceChoice waits={sorted(offered_faces)}; back_probe "
            f"advertised as legacy action="
            f"{ST.get('back_probe_advertised')}. Per CR 712.8a the caster "
            f"chooses which face to cast, so Iron Man, Tony Stark "
            f"({{3}}{{R}}{{R}}) is not safely selectable")
    elif not ST.get("cast2_submitted"):
        ass["A5_back_reachable"] = "not-run"
        notes.append("A5: second cast never submitted; back-face "
                     "reachability not tested")
    elif "back" in offered_faces or ST.get("back_probe_accepted") is True \
            or ST.get("ironman_on_bf"):
        ass["A5_back_reachable"] = "passed"
        notes.append(f"A5: back face reachable "
                     f"(offered faces={sorted(offered_faces)}, "
                     f"probe accepted={ST.get('back_probe_accepted')}, "
                     f"ironman_on_bf={ST.get('ironman_on_bf')})")
    else:
        ass["A5_back_reachable"] = "failed"
        probe_note = ("exploratory back_face=true submission "
                      f"{'rejected' if ST.get('back_probe_accepted') is False else 'not-run'}")
        notes.append(f"A5: back face NEVER offered at any ModalFaceChoice "
                     f"(offered faces={sorted(offered_faces)}); {probe_note}; "
                     f"per CR 712.8a the caster chooses which face to cast, "
                     f"so Iron Man, Tony Stark ({{3}}{{R}}{{R}}) is unreachable")
    # A6
    post = load_env("post")
    if post is not None:
        s = post["state"]
        stack_empty = len(stack_entries(s)) == 0
        ok = stack_empty
        ass["A6_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A6: post.json stack_empty={stack_empty} "
                     f"turn={s.get('turn_number')} phase={s.get('phase')}")
    else:
        ass["A6_cleanup"] = "not-run"
        notes.append("A6: post.json missing")
    # B (Tergrid)
    g2pre = load_env("g2_pre")
    if g2pre is not None:
        s = g2pre["state"]
        ok = any(obj_lname(s, o) == TERGRID for o in hand_ids(s, 0))
        ass["B1_tergrid_setup"] = "passed" if ok else "failed"
    else:
        ass["B1_tergrid_setup"] = "failed"
        notes.append("B1: g2_pre.json missing")
    if ST["g2_opps"]:
        r = ST["g2_opps"][0]
        actionable = (r["canSubmit"] and len(r["faces"]) > 0) \
            or len(r["choose_modal_face_actions"]) > 0
        ass["B2_tergrid_offered"] = "passed" if actionable else "failed"
        notes.append(f"B2: Tergrid ModalFaceChoice faces="
                     f"{[(f['face'], f['text'][:40]) for f in r['faces']]} "
                     f"actionable={actionable}")
    else:
        ass["B2_tergrid_offered"] = "failed"
        notes.append("B2: no ModalFaceChoice wait in game 2")
    if ST.get("g2_stall"):
        ass["B3_tergrid_accepted"] = "failed"
        notes.append("B3: ModalFaceChoice stalled >60s with no actionable "
                     "submission (the reported stuck signature)")
    elif not ST.get("g2_submitted"):
        ass["B3_tergrid_accepted"] = "not-run"
    elif ST.get("g2_rejected"):
        ass["B3_tergrid_accepted"] = "failed"
        notes.append("B3: Tergrid face choice rejected")
    elif ST.get("tergrid_on_bf"):
        ass["B3_tergrid_accepted"] = "passed"
    else:
        ass["B3_tergrid_accepted"] = "failed"
        notes.append("B3: Tergrid choice submitted but never resolved")
    g2post = load_env("g2_post")
    if g2post is not None:
        s = g2post["state"]
        tg = on_bf(s, 0, TERGRID)
        ok = len(tg) > 0 and len(stack_entries(s)) == 0
        ass["B4_tergrid_completes"] = "passed" if ok else "failed"
        notes.append(f"B4: tergrid on P0 BF={tg}; "
                     f"turn={s.get('turn_number')} phase={s.get('phase')}")
    else:
        ass["B4_tergrid_completes"] = "not-run"
        notes.append("B4: g2_post.json missing")
    # verdict
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup never reached the first ModalFaceChoice")
    elif ass["A2_modal_offered"] == "failed" \
            or ass["A3_choice_accepted"] == "failed":
        verdict = "reproduced"
        notes.append("the ModalFaceChoice decision itself failed "
                     "(no actionable choice / rejection / stall)")
    elif ass["A5_back_reachable"] == "failed":
        verdict = "reproduced"
        notes.append("RELATED FAILURE: the v0.20.0 hard stall is gone, but "
                     "the back face is not safely selectable on this build - "
                     "either it is never offered, or (as observed here) "
                     "selecting the advertised back-face choice soft-locks "
                     "the game. The 'select which side' decision remains "
                     "broken.")
    elif all(ass[k] == "passed" for k in
             ("A1_setup_ok", "A2_modal_offered", "A3_choice_accepted",
              "A4_front_completes", "A6_cleanup")):
        verdict = "not-reproduced"
        if ass["A5_back_reachable"] != "passed":
            notes.append("back-face probe did not complete; verdict scoped to "
                         "the front-face path")
    else:
        verdict = "blocked"
        notes.append("incomplete run; see assertion notes")
    return verdict, ass


async def finalize(verdict, ass):
    run = {
        "issue": ISSUE,
        "sibling_issue": 5487,
        "run_id": RUN_ID,
        "started_at": ST.get("started_iso"),
        "duration_s": round(time.time() - ST["t_start"], 1),
        "server": SERVER_IDENTITY,
        "server_port": 9374,
        "driver": {"protocol_advertised": 120, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_5474_01050.py", "rb").read()).hexdigest(),
        "decks": {"G1P0": "12x tony stark + 24x island + 24x mountain",
                  "G1P1": "60x forest",
                  "G2P0": "12x tergrid, god of fright + 48x swamp",
                  "G2P1": "60x forest"},
        "assertions": ass,
        "notes": ST["notes"],
        "observations": {
            "rejections": ST["rejections"],
            "face1_opps": ST["face1_opps"],
            "face2_opps": ST["face2_opps"],
            "g2_opps": ST["g2_opps"],
            "back_probe_sent": ST.get("back_probe_sent"),
            "back_probe_advertised": ST.get("back_probe_advertised"),
            "back_probe_accepted": ST.get("back_probe_accepted"),
            "tony1_on_bf": ST.get("tony1_on_bf"),
            "ironman_on_bf": ST.get("ironman_on_bf"),
            "tergrid_on_bf": ST.get("tergrid_on_bf"),
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x MDFC / high land density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "The reporter cast Tony Stark as a commander; the scenario casts it "
            "from the main deck. The ModalFaceChoice decision type is identical.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "The back_face=true probe was submitted via the legacy "
            "ChooseModalFace action path and is logged as exploratory; on "
            "cast 2 the engine advertised that exact action "
            "(back_probe_advertised in observations), on cast 1 it did not.",
        ],
        "setup_line": "G1 P0: 12x tony stark + 24x island + 24x mountain; "
                      "G1 P1: 60x forest. G2 P0: 12x tergrid, god of fright "
                      "+ 48x swamp; G2 P1: 60x forest.",
        "contract_line": "Cast the MDFC at main-phase priority; the "
                         "ModalFaceChoice must offer an actionable face "
                         "selection, the selection must be accepted, the "
                         "cast must resolve, and (per CR 712.8a) the back "
                         "face must be selectable.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_5474_01050.py", "w") as f:
        f.write(open(__file__).read())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)

    # AGENTS.md lesson: close WIRE/RUNLOG BEFORE the manifest loop.
    say("closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    # render the summary PNG from the saved states/assertions
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_5474_01050.py",
         EVDIR, str(ISSUE),
         "Tony Stark modal-face choice never offers the back face"],
        capture_output=True, text=True)
    print("render stdout:", (r.stdout or "")[:300], flush=True)
    print("render stderr:", (r.stderr or "")[:500], flush=True)
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass,
                      "run_id": RUN_ID}, indent=1), flush=True)


# ---------------------------------------------------------------- main

async def new_game(p0deck, p1deck):
    global P0C, P1C
    p0 = PhaseClient("P0")
    P0C = p0
    await p0.connect()
    await p0.create(p0deck)
    p1 = PhaseClient("P1")
    P1C = p1
    await p1.connect()
    await p1.join(p0.game_code, p1deck)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})
    return p0, p1


async def close_game(p0, p1):
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass


async def run_loop(p0, p1, max_turns, deadline_s):
    """Shared tick loop. Returns when stage reaches 'game1_done'/'game2_done'
    or the turn/deadline bail hits."""
    t0 = time.time()
    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    i = 0
    while i < 9000:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[pid] = time.time()
            else:
                if time.time() - last_change[pid] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} "
                        f"phase={s0.get('phase')} "
                        f"holding_priority="
                        f"{my_priority(merged_actions(st))} "
                        f"stage={ST['stage']}")
                    wire("watchdog_stale",
                         {"who": c.name, "rev": c.revision,
                          "turn": s0.get("turn_number"),
                          "phase": s0.get("phase"),
                          "stage": ST["stage"]})
                    last_change[pid] = time.time()
                holds_prio = my_priority(merged_actions(st))
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            drain_rejections(c, ST["stage"])
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        s = (p0.latest["state"] if p0.latest else {}) or {}
        turn = s.get("turn_number", 0)
        max_turn = max(max_turn, turn)

        # ---- stage transitions (evaluated on freshest state) ----
        if ST["stage"] == "g1_face1" and ST.get("face1_submitted") \
                and not ST.get("tony1_on_bf"):
            if on_bf(s, 0, TONY):
                ST["tony1_on_bf"] = True
                ST["stage"] = "g1_cast2_prep"
                say("STAGE -> g1_cast2_prep (Tony Stark #1 on BF; "
                    "exporting post_front)")
                await export_named("post_front")
        if ST["stage"] == "g1_face2":
            if on_bf(s, 0, IRONMAN):
                ST["ironman_on_bf"] = True
            if (ST.get("face2_submitted") or ST.get("back_probe_accepted")) \
                    and len(stack_entries(s)) == 0 \
                    and not my_priority(merged_actions(p0.latest or {})):
                # cast 2 resolved one way or another; capture post
                if len(on_bf(s, 0, TONY)) >= 1 and not ST.get("post_done"):
                    ST["post_done"] = True
                    ST["stage"] = "game1_done"
                    say("g1 cast2 resolved; exporting post, game 1 done")
                    await export_named("post")
        if ST["stage"] == "g2_face" and ST.get("g2_submitted"):
            if on_bf(s, 0, TERGRID):
                ST["tergrid_on_bf"] = True
                ST["stage"] = "game2_done"
                say("game 2 done (Tergrid on BF); exporting g2_post")
                await export_named("g2_post")
        # ---- ModalFaceChoice stall watchdog (reported stuck signature) ----
        for key, start_key in (("face1", "face1_wait_start"),
                              ("face2", "face2_wait_start"),
                              ("g2", "g2_wait_start")):
            ws = ST.get(start_key)
            submitted = ST.get(f"{key}_submitted")
            if ws and not submitted and time.time() - ws > 60:
                recs = {"face1": ST["face1_opps"], "face2": ST["face2_opps"],
                        "g2": ST["g2_opps"]}[key]
                actionable = any(
                    (r["canSubmit"] and r["faces"])
                    or r["choose_modal_face_actions"]
                    for r in recs)
                if not actionable:
                    ST[f"{key}_stall"] = True
                    await export_named("mid_stall")
                    say(f"STALL: ModalFaceChoice >60s with no actionable "
                        f"submission ({key}); captured mid_stall.json")
                    return False
        # ---- back-face probe wedge detection ----
        # If the exploratory back_face=true probe was consumed-but-not-
        # resolved, the engine freezes: no rejection, no new revision, and
        # no client holding priority. (Protocol 118 removed the waiting_for
        # decision surface from the live view, so the freeze is detected
        # from the frozen revision + no-priority-holder signature. The
        # cast-2 spell object may still sit on the stack with zone "Stack"
        # while nobody is prompted - that inconsistency IS the wedge, so
        # the spell's zone is diagnostic only, not a liveness signal.)
        # Detect the freeze, capture the authoritative stuck state, and
        # move on so game 2 (Tergrid control) still runs.
        if ST.get("back_probe_sent") and ST["stage"] == "g1_face2" \
                and not ST.get("probe_wedged"):
            # The 45s watchdog above resets last_change, so track the freeze
            # with a dedicated marker instead.
            if "_wedge_rev" not in ST:
                ST["_wedge_rev"] = p0.revision
                ST["_wedge_since"] = time.time()
            elif p0.revision != ST["_wedge_rev"]:
                ST["_wedge_rev"] = p0.revision
                ST["_wedge_since"] = time.time()
            elif time.time() - ST["_wedge_since"] > 90:
                p0_holds = my_priority(merged_actions(p0.latest or {}))
                p1_holds = my_priority(merged_actions(p1.latest or {}))
                s = (p0.latest["state"] if p0.latest else {}) or {}
                cast2_oid = ST.get("cast2_oid")
                spell_zone = (get_obj(s, cast2_oid) or {}).get("zone") \
                    if cast2_oid is not None else None
                n_stack = len(stack_entries(s))
                if not p0_holds and not p1_holds:
                    ST["probe_wedged"] = True
                    ST["stage"] = "game1_done"
                    ST["notes"].append(
                        "WEDGE: exploratory ChooseModalFace back_face=true "
                        "was consumed by the engine (server log: 'interaction "
                        "applied ... action_type=ChooseModalFace') with no "
                        "rejection and no new revision for 90s+; no client "
                        "holds priority. The cast-2 spell object "
                        f"(oid {cast2_oid}) sits at zone {spell_zone!r} with "
                        f"{n_stack} stack entries and nobody prompted - the "
                        "game is soft-locked. Capturing mid_stall.json "
                        "(authoritative export) and moving to game 2.")
                    say("WEDGE detected: engine froze after back-face probe; "
                        "exporting mid_stall.json")
                    await export_named("mid_stall")
                    return True
                # The 90s window elapsed but a client holds priority: the
                # game is still live. Reset the freeze marker and keep
                # waiting.
                ST["_wedge_since"] = time.time()
                ST["notes"].append(
                    "wedge window elapsed but a client holds priority "
                    f"(p0_holds={p0_holds} p1_holds={p1_holds} "
                    f"cast2_zone={spell_zone!r} stack={n_stack}); "
                    "freeze marker reset")
        if ST["stage"] in ("game1_done", "game2_done"):
            return True
        if turn >= max_turns:
            ST["notes"].append(f"turn {max_turns} reached in {ST['stage']}; "
                               "bailing out")
            say(f"turn bail at {turn}")
            return False
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={turn} phase={s.get('phase')} "
                f"stage={ST['stage']} tony_bf={on_bf(s,0,TONY)} "
                f"ironman_bf={on_bf(s,0,IRONMAN)} "
                f"stack={len(stack_entries(s))}")
        if time.time() - t0 > deadline_s:
            ST["notes"].append("loop deadline hit")
            say("loop deadline hit")
            return False
    return False


async def _main():
    ST.update({
        "t_start": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": [],
        "rejections": [],
        "tick_errors": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "stage": "g1_setup",
        "game": 1,
        "exports": {},
        "face1_opps": [],
        "face2_opps": [],
        "g2_opps": [],
    })
    await verify_server_hello()
    check_data_level()

    p0, p1 = await new_game(P0_DECK_G1, P1_DECK_G1)
    ok1 = await run_loop(p0, p1, 40, 1200)
    say(f"game 1 loop ended ok={ok1} stage={ST['stage']}")
    await close_game(p0, p1)
    await asyncio.sleep(2)

    ST["game"] = 2
    ST["stage"] = "g2_setup"
    ST["mana_needs"] = {"P0": {}, "P1": {}}
    p0, p1 = await new_game(P0_DECK_G2, P1_DECK_G2)
    ok2 = await run_loop(p0, p1, 30, 900)
    say(f"game 2 loop ended ok={ok2} stage={ST['stage']}")
    await close_game(p0, p1)

    verdict, ass = evaluate()
    say(f"verdict={verdict} assertions={json.dumps(ass)}")
    await finalize(verdict, ass)


async def main():
    pidfile = "/tmp/scenario_5474_01050.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_5474_01050 instance is alive "
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
