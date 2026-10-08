#!/usr/bin/env python3
"""Issue #6963: Elder Brain drops its land-play permission - compound
"play lands and cast spells" emits only the cast half.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Elder Brain ({5}{B}{B} Creature - ... 6/6):
    "Menace
     Whenever this creature attacks a player, exile all cards from that
     player's hand, then they draw that many cards. You may play lands and
     cast spells from among the exiled cards for as long as they remain
     exiled. If you cast a spell this way, you may spend mana as though it
     were mana of any color to cast it."

Reported symptom: the parsed attack trigger emits only the CAST half of the
compound permission. The land half is an explicit `Unimplemented` node, so a
player who attacks with Elder Brain can cast the exiled spells but cannot
play the exiled lands.

Parse state on v0.103.0 (observed 2026-10-08 before the run):
  triggers[0] (mode Attacks) subtree:
    .../execute/sub_ability/sub_ability =
        Unimplemented{name:"unrecognized_clause_head",
                      description:"play lands"} (optional:true)
    .../execute/sub_ability/sub_ability/sub_ability =
        GrantCastingPermission{permission:{type:PlayFromExile,
        duration:Permanent, granted_to:0, mode:Cast,
        mana_spend_permission:AnyColor}, target:TrackedSet{id:0}}
        with duration ForAsLongAs{Unrecognized("they remain exiled")}.
  No Play-mode permission node anywhere in the subtree.

Controls / class (from the issue + triage comment):
  - The Omenkeel ("You may play lands from among those cards...")
    emits CastFromZone{mode:Play} - standalone land-play clauses lower
    correctly.
  - Gix, Yawgmoth Praetor ("{4}{B}{B}{B}, Discard X cards: Exile the top X
    cards of target opponent's library. You may play lands and cast spells
    from among cards exiled this way without paying their mana costs.")
    shows the same dropped-play-half shape (duplicate #6952).

This is an area:parser issue (classifier:unsupported-aspect). Per the
playbook's subsystem rule, the parse evidence (input Oracle text, pinned
build identity, emitted AST) IS the primary evidence; the game leg checks
whether the runtime delivers the cast half and withholds the land half,
as the issue describes.

Setup (native engine, two human-client seats, default Bo1):
  P0: 12x Elder Brain, 12x Dark Ritual, 36x Swamp.
  P1: 30x Forest, 18x Island, 12x Lightning Bolt (holds everything;
      never plays, so the attack exiles a mixed hand).
  P0 ramps (rituals or natural 7 lands), casts Elder Brain, attacks P1
  (Menace vs an empty board). The trigger exiles P1's hand (lands +
  Bolts); P1 draws that many. P0 ACCEPTS the "you may play lands and cast
  spells" OptionalEffectChoice. On P0's next main phase the driver scans
  legal_actions for PlayLand offers on exiled lands (bug: absent) and
  CastSpell offers on exiled spells (control: present), then casts an
  exiled Lightning Bolt at P1 to test the cast half end to end.

Assertions:
  Parse (primary; measured on the pinned v0.103.0 card-data.json):
    A1_parse_drops_play   Elder Brain's attack-trigger subtree has NO
                          Play-mode permission over the exiled set.
                          passed => the reported defect persists.
    A2_parse_cast_correct  ... contains the Cast-mode permission with the
                          any-color mana rider over the tracked set.
                          passed => cast half typed as the issue says.
    A3_control_omenkeel    The Omenkeel emits a Play-mode permission gated
                          on Typed[Land]. passed => standalone land-play
                          clauses lower correctly.
    A4_class_gix           Gix's compound permission has NO Play-mode half.
                          passed => same defect (#6952 duplicate).
    A5_census              census of compound play+cast Oracle sentences and
                          their emitted modes. informational (always
                          recorded; "passed" = census completed).
  Game (runtime consequence of the parse defect):
    G1_attack_ok           Elder Brain attacked; P1's hand was exiled with
                          >=1 land among the exiled cards.
    G2_land_withheld       NO exiled land is offered as a legal PlayLand on
                          P0's main phase. passed => bug present at runtime.
    G3_cast_offered        an exiled spell is offered as a legal CastSpell.
                          passed => cast half works per the issue.
    G4_bolt_resolves       exiled Lightning Bolt casts at P1 for 3 damage
                          (any-color mana). passed.
  Supplementary (same trigger; recorded as related findings, not the
  reported defect):
    O1_exile_scope          only the attacked player's hand is exiled.
    O2_draw_count           P1 draws N == cards exiled from their hand.

Deliberate driver decisions: P0 ACCEPTS the "you may play lands and cast
spells" OptionalEffectChoice (accept=true choice); P1 holds lands/spells
(no land drops, no casts) so the attack exiles a mixed hand.

Verdict rule: blocked iff the card-data parse check itself cannot run.
  reproduced iff A1 passes (the emitted AST drops the Play half on the
  pinned release). not-reproduced iff A1 fails (the Play half is now
  emitted). A game that never reaches the attack leaves G1-G4 not-run but
  does not overturn a parse-level reproduction (the parse IS the reported
  outcome for this parser-class issue).

Evidence: evidence/6963/<run-id>/pre.json, mid.json, post.json,
parse_elder_brain.json, parse_gix.json, parse_omenkeel.json,
parse_census.json, data_evidence.json, run.json, assertions.json,
observations.json, manifest.sha256, summary.png,
scenario_6963_01030.py, wire_log.jsonl, scenario_run.log, server.log
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6963
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6963")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")


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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

PARSE = {"eb_play_modes": 0, "eb_cast_modes": 0, "eb_unimpl_play": 0,
         "eb_cast_anycolor": False, "ok1": False, "ok2": False,
         "ok3": False, "ok4": False, "census": None, "census_dropped": None}


def walk_perm_nodes(card):
    """Collect (path, kind, mode, node) for permission-ish AST nodes."""
    out = []

    def walk(n, path=""):
        if isinstance(n, dict):
            eff = n.get("effect")
            if isinstance(eff, dict):
                et = eff.get("type")
                if et in ("GrantCastingPermission", "CastFromZone"):
                    mode = eff.get("mode") or (eff.get("permission") or {}).get("mode")
                    out.append((path, et, mode, eff))
                elif et == "Unimplemented":
                    out.append((path, "Unimplemented", eff.get("name"), eff))
            for k, v in n.items():
                walk(v, path + "/" + str(k))
        elif isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, f"{path}[{i}]")

    walk(card)
    return out


def perm_modes(card):
    """(play_modes, cast_modes, unimplemented_play) summary for a card."""
    plays, casts, unimpl = [], [], []
    for path, kind, mode, node in walk_perm_nodes(card):
        if kind == "Unimplemented" and (mode == "play" or
                                       "play lands" in str(node.get("description") or "")):
            unimpl.append((path, node.get("description")))
        elif mode == "Play":
            plays.append((path, kind))
        elif mode == "Cast":
            casts.append((path, kind))
    return plays, casts, unimpl


def dump_card_parse(name, card, filename):
    with open(f"{EVDIR}/{filename}", "w") as f:
        json.dump({"card": name,
                   "oracle_text": card.get("oracle_text"),
                   "abilities": card.get("abilities"),
                   "triggers": card.get("triggers"),
                   "static_abilities": card.get("static_abilities")},
                  f, indent=1, default=str)
    say(f"saved {filename}")


def check_parse():
    """Record the v0.103.0 parse of Elder Brain / Omenkeel / Gix plus the
    compound-permission census.

    A1 contract: Elder Brain's attack-trigger subtree emits NO Play-mode
    permission (the reported defect persists). A2: it emits the Cast-mode
    GrantCastingPermission with the any-color mana rider. A3: The Omenkeel
    emits a Play-mode permission gated on Typed[Land]. A4: Gix's compound
    permission likewise emits NO Play-mode half (same defect, #6952 dup).
    A5: census completes (informational).
    """
    eb = CARD_DATA.get("elder brain", {})
    dump_card_parse("Elder Brain", eb, "parse_elder_brain.json")
    plays, casts, unimpl = perm_modes(eb)
    anycolor = any(
        (n.get("permission", {}) or {}).get("mana_spend_permission")
        in ("AnyColor", "AnyTypeOrColor")
        for _, _, _, n in walk_perm_nodes(eb)
        if isinstance(n, dict) and n.get("type") == "GrantCastingPermission")
    PARSE["eb_play_modes"] = len(plays)
    PARSE["eb_cast_modes"] = len(casts)
    PARSE["eb_unimpl_play"] = len(unimpl)
    PARSE["eb_cast_anycolor"] = anycolor
    PARSE["ok1"] = len(plays) == 0
    PARSE["ok2"] = len(casts) >= 1 and anycolor
    say(f"Elder Brain: play_modes={len(plays)} cast_modes={len(casts)} "
        f"unimpl_play={len(unimpl)} anycolor={anycolor}")
    wire("parse_elder_brain", {"play_modes": len(plays),
                              "cast_modes": len(casts),
                              "unimplemented_play": len(unimpl),
                              "anycolor": anycolor})

    om = CARD_DATA.get("the omenkeel", {})
    dump_card_parse("The Omenkeel", om, "parse_omenkeel.json")
    om_plays, om_casts, om_unimpl = perm_modes(om)
    om_land_gated = any(
        "Typed" in json.dumps(n) and "Land" in json.dumps(n)
        for _, _, _, n in walk_perm_nodes(om)
        if isinstance(n, dict)
        and (n.get("mode") == "Play"
             or (n.get("permission", {}) or {}).get("mode") == "Play"))
    PARSE["ok3"] = len(om_plays) >= 1 and om_land_gated
    say(f"Omenkeel: play_modes={len(om_plays)} land_gated={om_land_gated}")
    wire("parse_omenkeel", {"play_modes": len(om_plays),
                           "land_gated": om_land_gated})

    gx = CARD_DATA.get("gix, yawgmoth praetor", {})
    dump_card_parse("Gix, Yawgmoth Praetor", gx, "parse_gix.json")
    gx_plays, gx_casts, gx_unimpl = perm_modes(gx)
    PARSE["ok4"] = len(gx_plays) == 0
    say(f"Gix: play_modes={len(gx_plays)} cast_modes={len(gx_casts)} "
        f"unimpl_play={len(gx_unimpl)}")
    wire("parse_gix", {"play_modes": len(gx_plays),
                       "cast_modes": len(gx_casts),
                       "unimplemented_play": len(gx_unimpl)})

    census = []
    for name, card in CARD_DATA.items():
        ot = card.get("oracle_text") or ""
        for sent in re.split(r"\.\s*", ot):
            s = sent.lower()
            if ("play" in s and "land" in s and "cast" in s and "spell" in s
                    and "you may" in s):
                plays, casts, unimpl = perm_modes(card)
                census.append({
                    "card": name,
                    "sentence": sent.strip()[:160],
                    "play_modes": len(plays),
                    "cast_modes": len(casts),
                    "unimplemented_play": len(unimpl),
                })
                break
    census.sort(key=lambda e: e["card"])
    dropped = [c["card"] for c in census if c["play_modes"] == 0]
    with open(f"{EVDIR}/parse_census.json", "w") as f:
        json.dump({"generated_from":
                   f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json",
                   "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
                   "criterion": "oracle sentence contains you may + play + "
                                "land + cast + spell",
                   "cards": census}, f, indent=1)
    PARSE["census"] = len(census)
    PARSE["census_dropped"] = dropped
    say(f"census: {len(census)} compound cards; Play half missing on "
        f"{len(dropped)}")
    wire("parse_census", {"n": len(census), "dropped": dropped})

    out = {"elder_brain": {"play_modes": PARSE["eb_play_modes"],
                          "cast_modes": PARSE["eb_cast_modes"],
                          "unimplemented_play": PARSE["eb_unimpl_play"],
                          "cast_anycolor": anycolor,
                          "defect_persists": PARSE["ok1"]},
           "omenkeel_play_ok": PARSE["ok3"],
           "gix_defect_persists": PARSE["ok4"],
           "census_n": len(census), "census_dropped": dropped}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    say(f"data-level: A1_drops_play={PARSE['ok1']} "
        f"A2_cast_correct={PARSE['ok2']} A3_omenkeel={PARSE['ok3']} "
        f"A4_gix={PARSE['ok4']}")
    return out


BRAIN_T = "Elder Brain"
BRAIN_L = "elder brain"
RITUAL_T = "Dark Ritual"
RITUAL_L = "dark ritual"
SWAMP_L = "swamp"
FOREST_L = "forest"
ISLAND_L = "island"
BOLT_T = "Lightning Bolt"
BOLT_L = "lightning bolt"
LANDS = (SWAMP_L, FOREST_L, ISLAND_L)

P0_DECK = ((BRAIN_T, 12), (RITUAL_T, 12), ("Swamp", 36))
P1_DECK = (("Forest", 30), ("Island", 18), (BOLT_T, 12))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"brain_cast": False, "brain_cast_turn": None, "brain_oid": None,
      "rituals_cast": 0,
      "attack_declared": False, "attack_turn": None,
      "trigger_resolved": False, "trigger_at": None,
      "optional_accepted": False,
      "p1_hand_at_attack": None,
      "exile_snapshot": None, "mid_exported": False,
      "pre_exported": False, "scan_done": False,
      "scan_playland_offers": [], "scan_castspell_offers": [],
      "bolt_cast": False, "bolt_done": False,
      "p1_life_before": None, "p1_life_after": None,
      "post_at": None, "post_exported": False,
      "target_sels": []}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "target_selections": [],
       "offer_scans": [], "exile_sets": []}

# ------------------------------------------------------- state helpers

def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and str(o.get("controller")) == str(pid):
            if lname is None or obj_lname(state, oid) == lname:
                out.append(int(oid))
    return out


def exile_oids(state, owner=None, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Exile":
            continue
        if owner is not None and str(o.get("owner", o.get("controller", ""))) != str(owner):
            continue
        if lname is not None and obj_lname(state, oid) != lname:
            continue
        out.append(int(oid))
    return out


def is_land(o):
    nm = str(o.get("base_name") or o.get("name") or "").lower()
    return nm in LANDS


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and str(o.get("controller")) == str(pid)
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


def stack_empty(state):
    return not (state.get("stack") or [])


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


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_oid(ch):
    ref = cand_reference(ch)
    try:
        return str(int(ref))
    except (TypeError, ValueError):
        return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(op):
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": STAGE["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": STAGE["stage"],
                                "response": sub.get("response")})
    LAST_IID["iid"] = sub.get("interactionId")
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
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in LANDS)
    need = 3 if pid == 0 else 2
    keep = n_lands >= need or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        STAGE["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if STAGE["mulls"].get(tag, 0) <= 0:
        return False
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        spec = (rdata.get("spec", {}) or {})
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = cand_oid(ch)
            if ref is not None and is_land(get_obj(state, ref)):
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


def discard_rank(state, o, pid):
    nm = obj_lname(state, o)
    if pid == 1:
        # P1 must keep a mixed hand (lands + bolts) for the exile.
        # Discard the majority type first; ties -> discard lands.
        hand = hand_ids(state, 1)
        n_land = sum(1 for x in hand if is_land(get_obj(state, x)))
        n_bolt = sum(1 for x in hand if obj_lname(state, x) == BOLT_L)
        if nm == BOLT_L:
            return 1 if n_bolt > n_land else 3
        if is_land(get_obj(state, o)):
            return 1 if n_land >= n_bolt else 3
        return 2
    if nm in LANDS:
        return 0
    if nm == BRAIN_L:
        return 1
    if nm == RITUAL_L:
        return 2
    return 3


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = cand_oid(ch)
            if ref is not None:
                ref_of[ref] = ch["id"]
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o, pid),
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


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out

# ------------------------------------------------- issue-specific prompts

def iter_target_opps(st):
    """Yield (opp, rtype, spec_type) for every viewer_interaction
    opportunity that looks like a target selection."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                yield opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(cc for cc in surf_codes(ch) if cc)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(cc in codes for cc in ("candidate", "target")):
                yield opp, "exactChoices", "choose"


def record_target_sel(state, opp, stage):
    """Record a target-selection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["target_sels"]):
        return False
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in cands:
        oid = cand_oid(ch)
        o = get_obj(state, oid) if oid else {}
        cand_info.append({
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    ST["target_sels"].append(rec)
    OBS["target_selections"].append(rec)
    n = len(ST["target_sels"])
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"], x["seat"])
                         for x in cand_info]})
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"seat={x['seat']})" for x in cand_info[:8]))
    return True


def pick_player1(state, opp):
    """Exiled Bolt: P1 the player (face)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        if cand_seat(ch) == 1:
            return ch
        oid = cand_oid(ch)
        if oid is not None:
            o = get_obj(state, oid)
            if o.get("zone") == "Player" or str(o.get("controller", "")) == "1" \
                    and o.get("zone") not in ("Battlefield", "Graveyard",
                                              "Hand", "Library", "Exile",
                                              "Stack"):
                return ch
        blob = (choice_text(ch) + " " + json.dumps(ch, default=str)).lower()
        if "player" in blob and ("opponent" in blob or "you" in blob):
            return ch
    return None


async def answer_target(c, state, opp, rtype, spec_type, ch, tag, stage):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    oid = cand_oid(ch)
    say(f"[{tag}] answering {stage} target: "
        f"{obj_lname(state, oid) if oid else choice_text(ch)[:40]} "
        f"(oid {oid}) via {sub['response']['type']}")
    wire("target_answer", {"who": tag, "stage": stage, "iid": iid,
                           "oid": oid, "submission": sub})
    await answer_vi(c, opp, ch, tag)
    return oid


async def bolt_target_tick(c, tag, st, state):
    """Answer the exiled-Bolt target prompt: P1's face."""
    if ST["bolt_cast"] and not ST["bolt_done"]:
        for opp, rtype, spec_type in iter_target_opps(st):
            record_target_sel(state, opp, "exiled_bolt")
            pick = pick_player1(state, opp)
            if pick is None:
                say(f"[{tag}] bolt prompt has no P1 candidate; not answering")
                OBS["unexpected_prompts"].append(
                    {"who": tag, "stage": "exiled_bolt",
                     "note": "no P1 candidate"})
                return False
            await answer_target(c, state, opp, rtype, spec_type, pick, tag,
                                "exiled_bolt")
            ST["bolt_done"] = True
            return True
    return False


def find_optional_opps(st):
    """vi exactChoices opportunities carrying a decideOptionalEffect code."""
    out = []
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(cc for cc in surf_codes(ch) if cc)
        if "decideOptionalEffect" in codes:
            out.append((opp, chs))
    return out


def accept_value_of(ch):
    """Return the 'accept' value surface ('true'/'false') of a choice."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("role") == "accept" \
                and "value" in d:
            return str(d.get("value")).lower()
    return None


async def optional_accept_tick(c, tag, st, state):
    """ACCEPT the 'you may play lands and cast spells' optional choice.

    106 shape (proven on scenario_301_01030): vi exactChoices with a
    decideOptionalEffect action code; accept = the choice whose surface has
    role "accept" and value "true".
    """
    if ST["optional_accepted"]:
        return False
    for opp, chs in find_optional_opps(st):
        iid = opp.get("interactionId")
        wire("optional_opportunity",
             {"who": tag, "iid": iid,
              "choices": [{"id": ch.get("id"),
                           "accept": accept_value_of(ch),
                           "text": choice_text(ch)[:80]} for ch in chs],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        pick = next((ch for ch in chs if accept_value_of(ch) == "true"), None)
        if pick is None:
            say(f"[{tag}] optional prompt has no accept=true choice; "
                f"leaving unanswered")
            OBS["unexpected_prompts"].append(
                {"who": tag, "stage": "optional",
                 "note": "no accept=true choice"})
            return False
        ST["optional_accepted"] = True
        say(f"[{tag}] ACCEPTING optional 'play lands and cast spells'")
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def do_declare(c, acts, st, pid, tag):
    """DeclareAttackers (P0 attacks with the Elder Brain) or
    DeclareBlockers (empty)."""
    state = st["state"]
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            brain = None
            brains = bf_oids(state, 0, BRAIN_L)
            if brains:
                brain = brains[0]
            turn = state.get("turn_number")
            ready = (brain is not None
                     and ST["brain_cast_turn"] is not None
                     and turn is not None
                     and turn > ST["brain_cast_turn"]
                     and not ST["attack_declared"]
                     and state.get("active_player") == 0)
            if ready:
                # 106 precedent: attacks=[[int(oid), {type:Player, data:1}]]
                d["data"]["attacks"] = [[int(brain),
                                         {"type": "Player", "data": 1}]]
                d["data"]["bands"] = []
                ST["attack_declared"] = True
                ST["attack_turn"] = turn
                ST["p1_hand_at_attack"] = len(hand_ids(state, 1))
                say(f"[{tag}] attacking P1 with Elder Brain oid={brain} "
                    f"(turn {turn}); P1 hand={ST['p1_hand_at_attack']}")
                wire("attack", {"brain_oid": brain, "turn": turn,
                               "p1_hand": ST["p1_hand_at_attack"]})
            else:
                d["data"]["attacks"] = []
                d["data"]["bands"] = []
                say(f"[{tag}] declare no attackers")
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
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
                                                   "data": {"choiceId":
                                                             ch.get("id")}}},
                                  c.name)
                return True
    return False


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    return a
            except (TypeError, ValueError):
                pass
    return None


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]

# ------------------------------------------------------------- seat ticks

async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_declare(c, acts, st, 0, tag):
        return True
    if await bolt_target_tick(c, tag, st, state):
        return True
    if await optional_accept_tick(c, tag, st, state):
        return True

    # track the Elder Brain on the battlefield
    brains = bf_oids(state, 0, BRAIN_L)
    if brains and ST["brain_oid"] is None:
        ST["brain_oid"] = brains[0]
        ST["brain_cast_turn"] = state.get("turn_number")
        say(f"[{tag}] Elder Brain on BF: oid={brains[0]} "
            f"(turn {ST['brain_cast_turn']})")
        wire("brain_entered", {"oid": brains[0]})

    # MID: the attack trigger resolved -> exile snapshot (P1's hand gone)
    if (ST["attack_declared"] and not ST["trigger_resolved"]
            and exile_oids(state, owner=1)):
        ST["trigger_resolved"] = True
        ST["trigger_at"] = time.time()
        ex = [(o, obj_lname(state, o),
               str(get_obj(state, o).get("owner",
                                        get_obj(state, o).get("controller"))))
              for o in exile_oids(state)]
        ST["exile_snapshot"] = ex
        OBS["exile_sets"].append({"at": "trigger_resolved", "exile": ex})
        say(f"[{tag}] trigger resolved: {len(ex)} cards in exile: "
            + ", ".join(f"{n}(o{ow})" for _, n, ow in ex[:14]))
        wire("trigger_resolved", {"exile": ex})
        await do_export(c, "mid.json")
        ST["mid_exported"] = True
        say("MID exported: post-trigger exile snapshot")
        st = st_of(c) or st
        state = st["state"]
        acts = merged_actions(st)

    # PRE + offer scan: first P0 main phase after the trigger resolved,
    # optional accepted (or 30s elapsed without the optional surfacing),
    # stack empty.
    if (ST["trigger_resolved"] and not ST["pre_exported"]
            and my_main(state, 0)
            and (ST["optional_accepted"]
                 or time.time() - (ST["trigger_at"] or 0) > 30)):
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported: P0 post-attack main, optional="
            f"{ST['optional_accepted']}")
        st = st_of(c) or st
        state = st["state"]
        acts = merged_actions(st)
        exiled = set(str(o) for o in exile_oids(state))
        ex_lands = [o for o in exile_oids(state)
                    if is_land(get_obj(state, o))]
        for a in acts:
            src = str(a.get("_src_oid") or "")
            if a.get("type") == "PlayLand" and src in exiled:
                ST["scan_playland_offers"].append(
                    {"src_oid": src, "name": obj_lname(state, src)})
            if "cast" in a.get("type", "").lower() and src in exiled:
                ST["scan_castspell_offers"].append(
                    {"src_oid": src, "name": obj_lname(state, src),
                     "action": a.get("type")})
        ST["scan_done"] = True
        OBS["offer_scans"].append(
            {"turn": state.get("turn_number"),
             "exiled_lands": [(o, obj_lname(state, o)) for o in ex_lands],
             "playland_offers": ST["scan_playland_offers"],
             "castspell_offers": ST["scan_castspell_offers"]})
        say(f"[{tag}] offer scan: exiled_lands="
            f"{[(o, obj_lname(state, o)) for o in ex_lands]} "
            f"playland_offers={ST['scan_playland_offers']} "
            f"castspell_offers={ST['scan_castspell_offers']}")
        wire("offer_scan", OBS["offer_scans"][-1])

    # G4: cast an exiled Lightning Bolt at P1 (any-color mana rider)
    if (ST["scan_done"] and not ST["bolt_cast"] and my_main(state, 0)):
        exiled = set(str(o) for o in exile_oids(state))
        bolt_oid = next(
            (o for o in exile_oids(state) if obj_lname(state, o) == BOLT_L),
            None)
        if bolt_oid is not None:
            a = find_cast_action(acts, state, BOLT_L)
            src_ok = a is not None and str(a.get("_src_oid")) == str(bolt_oid)
            if src_ok:
                ST["bolt_cast"] = True
                ST["p1_life_before"] = life_of(state, 1)
                say(f"[{tag}] casting exiled Lightning Bolt oid={bolt_oid} "
                    f"at P1 (engine Auto payment, any-color rider)")
                wire("cast_submit", {"tag": "exiled_bolt", "oid": bolt_oid})
                await submit_as_is(c, a)
                return True

    # bolt resolution: P1 life drop
    if ST["bolt_cast"] and ST["bolt_done"] \
            and ST["p1_life_after"] is None:
        life = life_of(state, 1)
        if life is not None and ST["p1_life_before"] is not None \
                and life < ST["p1_life_before"]:
            ST["p1_life_after"] = life
            say(f"[{tag}] exiled Bolt resolved: P1 "
                f"{ST['p1_life_before']} -> {life}")
            wire("bolt_resolved", {"before": ST["p1_life_before"],
                                   "after": life})

    # POST: bolt resolved, stack empty, 8s grace
    if ST["p1_life_after"] is not None and not ST["post_exported"] \
            and stack_empty(state):
        if ST["post_at"] is None:
            ST["post_at"] = time.time()
    if (ST["post_at"] is not None and not ST["post_exported"]
            and time.time() - ST["post_at"] > 8
            and stack_empty(state)):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say("POST exported: exiled Bolt resolved; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_lnames(state, 0)
        n_untapped = len(untapped_lands(state, 0))
        # ramp: Dark Rituals first (greedy), then the Brain {5}{B}{B}
        if not ST["brain_cast"] and BRAIN_L in hn:
            if RITUAL_L in hn and n_untapped >= 1:
                a = find_cast_action(acts, state, RITUAL_L)
                if a is not None:
                    ST["rituals_cast"] += 1
                    say(f"[{tag}] casting Dark Ritual "
                        f"(#{ST['rituals_cast']}, engine Auto payment)")
                    wire("cast_submit", {"tag": "ritual"})
                    await submit_as_is(c, a)
                    return True
            a = find_cast_action(acts, state, BRAIN_L)
            if a is not None:
                ST["brain_cast"] = True
                say(f"[{tag}] casting Elder Brain (engine Auto payment)")
                wire("cast_submit", {"tag": "brain"})
                await submit_as_is(c, a)
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_declare(c, acts, st, 1, tag):
        return True

    # P1 holds everything: no land drops, no casts. Just pass priority.
    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ------------------------------------------------------------- main loop

async def main():
    t0 = time.time()
    last_rev_change = t0
    game_started = False

    hello = await verify_server_hello()
    data_level = check_parse()

    p0 = PhaseClient("P06963r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16963r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # (or holding an unanswered vi decision) with no revision
                # change (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"]})
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} pp={state.get('priority_player')} "
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"P0life={life_of(state, 0)} P1life={life_of(state, 1)} "
                f"brain={ST['brain_oid']} attacked={ST['attack_declared']} "
                f"trig={ST['trigger_resolved']} opt={ST['optional_accepted']} "
                f"scan={ST['scan_done']} bolt={ST['bolt_cast']}/"
                f"{ST['bolt_done']} post={ST['post_exported']}")

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    await finish(p0, p1, t0, hello, data_level)


async def finish(p0, p1, t0, hello, data_level):
    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_s = load("mid.json")
    post_s = load("post.json")

    # A1: parse drops the Play half (defect persists on v0.103.0)
    A["A1_parse_drops_play"] = "passed" if PARSE["ok1"] else "failed"
    D["A1_parse_drops_play"] = (
        f"Elder Brain Play-mode permission nodes: {PARSE['eb_play_modes']} "
        f"(expected 0; Unimplemented 'play lands' nodes: "
        f"{PARSE['eb_unimpl_play']})")

    # A2: cast half typed with the any-color rider
    A["A2_parse_cast_correct"] = "passed" if PARSE["ok2"] else "failed"
    D["A2_parse_cast_correct"] = (
        f"Cast-mode nodes: {PARSE['eb_cast_modes']}; any-color mana rider: "
        f"{PARSE['eb_cast_anycolor']}")

    # A3: Omenkeel control
    A["A3_control_omenkeel"] = "passed" if PARSE["ok3"] else "failed"
    D["A3_control_omenkeel"] = (
        "The Omenkeel emits a Play-mode permission gated on Typed[Land]: "
        f"{PARSE['ok3']} (standalone land-play clauses lower correctly)")

    # A4: Gix class (same defect)
    A["A4_class_gix"] = "passed" if PARSE["ok4"] else "failed"
    D["A4_class_gix"] = (
        f"Gix Play-mode permission nodes: 0 expected (same defect as Elder "
        f"Brain; #6952 duplicate): {PARSE['ok4']}")

    # A5: census completed
    A["A5_census"] = "passed" if PARSE["census"] is not None else "failed"
    D["A5_census"] = (
        f"{PARSE['census']} cards with compound 'you may play ... and cast "
        f"...' Oracle sentences; {len(PARSE['census_dropped'] or [])} emit "
        f"no Play-mode permission: {(PARSE['census_dropped'] or [])[:12]}")

    # pick the latest available state for game assertions
    gs = post_s or pre_s or mid_s

    # G1: attack happened and >=1 land exiled from P1's hand
    if gs is not None and ST["attack_declared"]:
        ex_lands_p1 = [o for o in exile_oids(gs, owner=1)
                       if is_land(get_obj(gs, o))]
        ok = len(ex_lands_p1) >= 1
        A["G1_attack_ok"] = "passed" if ok else "failed"
        D["G1_attack_ok"] = (
            f"attack_declared={ST['attack_declared']} "
            f"(turn {ST['attack_turn']}); P1-owned exiled lands: "
            f"{[(o, obj_lname(gs, o)) for o in ex_lands_p1]}")
    else:
        A["G1_attack_ok"] = "not-run"
        D["G1_attack_ok"] = (
            f"attack_declared={ST['attack_declared']}; "
            "no game state available" if gs is None
            else f"attack_declared={ST['attack_declared']} (never attacked)")

    # G2: no PlayLand offer on an exiled land (bug present at runtime)
    if ST["scan_done"]:
        ok = len(ST["scan_playland_offers"]) == 0
        A["G2_land_withheld"] = "passed" if ok else "failed"
        D["G2_land_withheld"] = (
            f"PlayLand offers on exiled lands: "
            f"{ST['scan_playland_offers']} (expected none)")
    else:
        A["G2_land_withheld"] = "not-run"
        D["G2_land_withheld"] = "offer scan never ran"

    # G3: an exiled spell offered as CastSpell
    if ST["scan_done"]:
        ok = len(ST["scan_castspell_offers"]) >= 1
        A["G3_cast_offered"] = "passed" if ok else "failed"
        D["G3_cast_offered"] = (
            f"CastSpell offers on exiled cards: "
            f"{ST['scan_castspell_offers']} (expected >=1)")
    else:
        A["G3_cast_offered"] = "not-run"
        D["G3_cast_offered"] = "offer scan never ran"

    # G4: exiled Bolt resolved for 3 damage
    if ST["p1_life_before"] is not None and ST["p1_life_after"] is not None:
        ok = ST["p1_life_after"] == ST["p1_life_before"] - 3
        A["G4_bolt_resolves"] = "passed" if ok else "failed"
        D["G4_bolt_resolves"] = (
            f"P1 {ST['p1_life_before']} -> {ST['p1_life_after']} "
            f"(expected -3)")
    else:
        A["G4_bolt_resolves"] = "not-run"
        D["G4_bolt_resolves"] = (
            f"bolt_cast={ST['bolt_cast']} bolt_done={ST['bolt_done']} "
            f"life {ST['p1_life_before']} -> {ST['p1_life_after']}")

    # O1: exile scope (supplementary)
    snap = ST["exile_snapshot"] or []
    if snap:
        others = [x for x in snap if x[2] != "1"]
        ok = len(others) == 0
        A["O1_exile_scope"] = "passed" if ok else "failed"
        D["O1_exile_scope"] = (
            f"{len(snap)} exiled cards; non-P1-owned: "
            f"{[(n, ow) for _, n, ow in others][:8]} (expected none)")
    else:
        A["O1_exile_scope"] = "not-run"
        D["O1_exile_scope"] = "no exile snapshot"

    # O2: draw count (supplementary)
    if (ST["p1_hand_at_attack"] is not None and snap and post_s is not None):
        n_exiled = sum(1 for _, _, ow in snap if ow == "1")
        hand_post = len(hand_ids(post_s, 1))
        drawn = hand_post - (ST["p1_hand_at_attack"] - n_exiled)
        ok = drawn == n_exiled
        A["O2_draw_count"] = "passed" if ok else "failed"
        D["O2_draw_count"] = (
            f"P1 hand at attack={ST['p1_hand_at_attack']}, exiled={n_exiled}, "
            f"hand in post={hand_post} -> drew {drawn} (expected {n_exiled})")
    else:
        A["O2_draw_count"] = "not-run"
        D["O2_draw_count"] = (
            f"p1_hand_at_attack={ST['p1_hand_at_attack']} "
            f"snapshot={bool(snap)} post={post_s is not None}")

    # ---------------------------------------------------------- verdict
    if PARSE["census"] is None:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: parse check could not run")
    elif A["A1_parse_drops_play"] == "passed":
        verdict = "reproduced"
        OBS["notes"].append(
            "verdict=reproduced: Elder Brain's compound permission still "
            "drops the Play half on v0.103.0 (parse-level defect)")
    elif A["A1_parse_drops_play"] == "failed":
        verdict = "not-reproduced"
        OBS["notes"].append(
            "verdict=not-reproduced: Elder Brain now emits a Play-mode "
            "permission on v0.103.0")
    else:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: incomplete assertion chain")

    for k in ("A1_parse_drops_play", "A2_parse_cast_correct",
              "A3_control_omenkeel", "A4_class_gix", "A5_census",
              "G1_attack_ok", "G2_land_withheld", "G3_cast_offered",
              "G4_bolt_resolves", "O1_exile_scope", "O2_draw_count"):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "target_selections": OBS["target_selections"],
                   "unexpected_prompts": OBS["unexpected_prompts"],
                   "auto_answered": OBS["auto_answered"],
                   "tick_errors": OBS["tick_errors"],
                   "rejections": OBS["rejections"],
                   "offer_scans": OBS["offer_scans"],
                   "exile_sets": OBS["exile_sets"],
                   "notes": OBS["notes"]}, f, indent=1, default=str)

    date_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "date": date_iso,
        "game_code": STAGE.get("game_code"),
        "server": {
            "server_version": hello.get("server_version"),
            "build_commit": hello.get("build_commit"),
            "protocol_version": hello.get("protocol_version"),
            "mode": hello.get("mode"),
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {"P0": [[n, c] for n, c in P0_DECK],
                  "P1": [[n, c] for n, c in P1_DECK]},
        "format_config": "default Bo1 (2 human-client seats)",
        "setup_line": ("P0 12x Elder Brain / 12x Dark Ritual / 36x Swamp; "
                       "P1 30x Forest / 18x Island / 12x Lightning Bolt "
                       "(holds everything). P0 ramps with Rituals, casts "
                       "Elder Brain {5}{B}{B} (engine Auto payment), attacks "
                       "P1 (Menace vs empty board). Trigger exiles P1's "
                       "hand; P1 draws that many; P0 ACCEPTS the 'you may "
                       "play lands and cast spells' optional. On P0's next "
                       "main the driver scans for PlayLand offers on exiled "
                       "lands and CastSpell offers on exiled spells, then "
                       "casts an exiled Bolt at P1 (any-color mana)."),
        "contract_line": ("Parse is primary (area:parser): Elder Brain's "
                          "attack-trigger subtree must still emit NO "
                          "Play-mode permission (Unimplemented 'play lands' "
                          "node + typed Cast half -> reproduced). Game leg: "
                          "exiled lands must NOT be offered as PlayLand "
                          "(bug at runtime); exiled spells must be offered "
                          "and the Bolt must resolve for 3."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6963.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; parse contract A1..A5 and "
            "the game plan unchanged.",
            "Parse leg runs before the game: walks the pinned "
            "card-data.json AST for permission nodes (GrantCastingPermission "
            "/ CastFromZone modes + Unimplemented 'play lands').",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably, so no driver mana taps (driver taps on top of "
            "engine auto-taps double-pay). Dark Rituals are cast greedily "
            "on the Brain turn, then the Brain via the offered cast action.",
            "DeclareAttackers via legacy Action with "
            "attacks=[[int(oid), {type:Player, data:1}]] (106 precedent); "
            "post-declare priority is passed, never held.",
            "The 'you may play lands and cast spells' optional is answered "
            "via the proven 106 shape (vi exactChoices + decideOptionalEffect, "
            "accept = role accept / value true).",
            "Offer scan runs on P0's first post-trigger main phase: every "
            "merged PlayLand/CastSpell action is checked against the exiled "
            "object set; PRE is exported immediately before the scan.",
            "Pre/mid/post states are authoritative exports (data.state "
            "parsed once from the export envelope) via the host client "
            "only; the reported OUTCOME is asserted on the saved "
            "states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5652574170,
        "limitations": [
            "Browser UI not exercised; this is an area:parser issue - the "
            "primary evidence is the emitted AST in the pinned card-data.json.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via full "
            "game replay).",
            "P1 deliberately holds lands/spells so the attack exiles a "
            "mixed hand; this is a fixture choice, not normal play.",
        ],
        "mulligans": STAGE["mulls"],
        "rejections": OBS["rejections"],
        "notes": OBS["notes"],
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"wrote run.json verdict={verdict}")

    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6963_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6963_01030.py into EVDIR")

    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied server.log into EVDIR")
    except Exception as e:
        say(f"server.log copy failed: {e} "
            f"(shared pinned server; run log kept in scenario_run.log)")

    render_summary(run, pre_s, mid_s, post_s)

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    # Final manifest AFTER RUNLOG is closed: write_manifest()'s own say()
    # is then stdout-only (guarded on closed RUNLOG), so no logged line can
    # go stale after the hash.
    write_manifest()
    say("scenario finished")


def render_summary(run, pre_s, mid_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 980
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6963 - Elder Brain land-play permission",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "compound play+cast permission",
           fill=(140, 160, 180))
    y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states / card-data):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse_drops_play": "card-data: Play half still dropped (Unimplemented)",
        "A2_parse_cast_correct": "card-data: Cast half typed + any-color rider",
        "A3_control_omenkeel": "card-data: Omenkeel Play-mode OK (control)",
        "A4_class_gix": "card-data: Gix drops Play half too (#6952 dup)",
        "A5_census": "census of compound play+cast sentences completed",
        "G1_attack_ok": "game: Brain attacked, >=1 land exiled from P1",
        "G2_land_withheld": "game: NO PlayLand offer on exiled lands",
        "G3_cast_offered": "game: exiled spell offered as CastSpell",
        "G4_bolt_resolves": "game: exiled Bolt resolved for 3 damage",
        "O1_exile_scope": "obs: only P1's hand exiled",
        "O2_draw_count": "obs: P1 drew N == exiled count",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Exile / offers across states:", fill=(200, 210, 225))
    y += 24
    for label, st in (("pre ", pre_s), ("mid ", mid_s), ("post", post_s)):
        if st is not None:
            ex = exile_oids(st)
            line = (f"{label}: exile_n={len(ex)} "
                    + " ".join(obj_lname(st, o) for o in ex[:10]))
        else:
            line = f"{label}: (no state)"
        d.text((36, y), line[:112], fill=(150, 160, 175))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    notes = (run["notes"] or [])[:2]
    details = [run["assertion_details"].get(k, "") for k in labels]
    for n in notes + details:
        for seg in [str(n)[i:i + 116] for i in range(0, len(str(n)), 116)][:2]:
            d.text((36, y), seg, fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    import glob
    files = ["pre.json", "mid.json", "post.json",
             "parse_elder_brain.json", "parse_omenkeel.json",
             "parse_gix.json", "parse_census.json",
             "data_evidence.json", "run.json", "assertions.json",
             "observations.json", "scenario_6963_01030.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]
    files += sorted(os.path.basename(p)
                    for p in glob.glob(f"{EVDIR}/target_sel_*.json"))
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
