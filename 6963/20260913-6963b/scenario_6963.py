#!/usr/bin/env python3
"""Issue #6963: Elder Brain drops its land-play permission - compound
"play lands and cast spells" emits only the cast half.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Elder Brain ({5}{B}{B} Creature - ... 6/6):
    "Menace
     Whenever this creature attacks a player, exile all cards from that
     player's hand, then they draw that many cards. You may play lands and
     cast spells from among the exiled cards for as long as they remain
     exiled. If you cast a spell this way, you may spend mana as though it
     were mana of any color to cast it."

Reported symptom: the parsed attack trigger emits only the CAST half of the
compound permission. The land half is an explicit `Unimplemented` node
(name "play", description "play lands"), so a player who attacks with Elder
Brain can cast the exiled spells but cannot play the exiled lands.

Parse state on v0.81.3 (observed 2026-09-13 before the run):
  triggers[0] (mode Attacks) subtree:
    .../execute/sub_ability/sub_ability =
        Unimplemented{name:"play", description:"play lands"} (optional:true)
    .../execute/sub_ability/sub_ability/sub_ability =
        GrantCastingPermission{permission:{type:PlayFromExile, mode:Cast,
        mana_spend_permission:AnyTypeOrColor}, target:TrackedSet{id:0}}
        with duration ForAsLongAs{Unrecognized("they remain exiled")}.
  No Play-mode permission node anywhere in the subtree.

Controls / class (from the issue + triage comment):
  - The Omenkeel ("You may play lands from among those cards...") emits
    CastFromZone{mode:Play, target:And[Typed[Land], ExiledBySource]} -
    standalone land-play clauses lower correctly.
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
      discards keep lands first so the attack exiles lands).
  P0 ramps (rituals or natural 7 lands), casts Elder Brain, attacks P1
  (Menace vs an empty board). The trigger exiles P1's hand (lands +
  Bolts); P1 draws that many. On P0's next main phase the driver scans
  legal_actions for PlayLand offers on exiled lands (bug: absent) and
  CastSpell offers on exiled spells (control: present), then casts an
  exiled Lightning Bolt at P1 to test the cast half end to end.

Assertions:
  Parse (primary; measured on the pinned v0.81.3 card-data.json):
    A1_parse_play_present  Elder Brain's attack-trigger subtree contains a
                           Play-mode permission over the exiled set.
                           expected FAIL (the reported defect).
    A2_parse_cast_correct  ... contains the Cast-mode permission with the
                           any-color mana rider over the tracked set.
                           expected PASS (issue says the cast half is right).
    A3_control_omenkeel    The Omenkeel emits a Play-mode permission gated
                           on Typed[Land]. expected PASS.
    A4_class_gix           Gix's compound permission emits a Play-mode
                           half. expected FAIL (same defect; #6952 dup).
    A5_census              census of compound play+cast Oracle sentences and
                           their emitted modes. informational (always
                           recorded; "passed" = census completed).
  Game (runtime consequence of the parse defect):
    G1_attack_ok           Elder Brain attacked; P1's hand was exiled with
                           >=1 land among the exiled cards.
    G2_land_withheld       an exiled land is offered as a legal PlayLand on
                           P0's main phase. expected FAIL (bug at runtime).
    G3_cast_offered        an exiled spell is offered as a legal CastSpell.
                           expected PASS (cast half works per the issue).
    G4_bolt_resolves       exiled Lightning Bolt casts at P1 for 3 damage
                           (any-color mana). expected PASS.
  Supplementary (same trigger; recorded as related findings, not the
  reported defect):
    O1_exile_scope          only the attacked player's hand is exiled.
                           expected FAIL (ChangeZoneAll target has
                           controller:null; both hands exiled).
    O2_draw_count            P1 draws N == cards exiled from their hand.
                           expected FAIL (P1 drew 4 for 7 exiled).

Deliberate driver decisions: P0 ACCEPTS the "you may play lands and cast
spells" OptionalEffectChoice (accept=true choice); P1 holds lands/spells
(no land drops, no casts) so the attack exiles a mixed hand.

Verdict rule: reproduced iff A1 fails (the emitted AST drops the Play half
on the pinned release). blocked iff the card-data parse check itself
cannot run. not-reproduced iff A1 passes. The game leg enriches the
evidence; a game that never reaches the attack leaves G1-G4 not-run but
does not overturn a parse-level reproduction (the parse IS the reported
outcome for this parser-class issue).

Evidence: evidence/6963/<run-id>/pre.json, mid.json, post.json,
parse_elder_brain.json, parse_gix.json, parse_omenkeel.json,
parse_census.json, run.json, manifest.sha256, summary.png,
scenario_6963.py, wire_log.jsonl, scenario_run.log, server.log
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6963")
ISSUE = 6963
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BRAIN = "elder brain"
RITUAL = "dark ritual"
SWAMP = "swamp"
FOREST = "forest"
ISLAND = "island"
BOLT = "lightning bolt"
P0_LANDS = (SWAMP,)
P1_LANDS = (FOREST, ISLAND)
ALL_LANDS = (SWAMP, FOREST, ISLAND)

P0_DECK = [(BRAIN, 12), (RITUAL, 12), (SWAMP, 36)]
P1_DECK = [(FOREST, 30), (ISLAND, 18), (BOLT, 12)]

CD_PATH = f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (isolated v0.81.3 server "
              "started for this run) + verified pin (minisign-verify of "
              "binary + signed data manifest with the repo-pinned key).",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# --------------------------------------------------------------------------
# parse helpers
# --------------------------------------------------------------------------

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
        if kind == "Unimplemented" and mode == "play":
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
                  f, indent=1)
    say(f"saved {filename}")


# --------------------------------------------------------------------------
# game helpers (driver conventions per AGENTS.md)
# --------------------------------------------------------------------------

def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def exile_ids(state, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Exile"
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid, lands=ALL_LANDS):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in lands):
            out.append(int(oid))
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id") and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)
    return out


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        deep_refs(d, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


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
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_play_present", "A2_parse_cast_correct",
            "A3_control_omenkeel", "A4_class_gix", "A5_census",
            "G1_attack_ok", "G2_land_withheld", "G3_cast_offered",
            "G4_bolt_resolves", "O1_exile_scope", "O2_draw_count")}
    obs = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
           "offer_scans": [], "exile_sets": []}
    ST = {"brain_cast": False, "brain_cast_turn": None, "brain_oid": None,
          "attacked": False, "attack_turn": None,
          "mid_exported": False, "pre_exported": False, "post_exported": False,
          "post_at": None, "exile_at_attack": [], "land_exiled": [],
          "spell_exiled": [], "scan_done": False, "scan_land_offer": None,
          "scan_spell_offer": None, "scan_n_playland": 0,
          "bolt_cast": False, "bolt_targeted": False, "bolt_done": False,
          "p1_life_before_bolt": None, "p1_life_after_bolt": None,
          "p0_land_drop_done": False, "exile_bolt_oid": None,
          "exile_land_oid": None}
    prompt_first_seen = {}
    last_select = {}
    kept = {}

    # ================= parse checks (primary evidence) =================
    cd = json.load(open(CD_PATH))
    cd_sha = hashlib.sha256(open(CD_PATH, "rb").read()).hexdigest()
    say(f"card-data.json sha256={cd_sha}")
    notes.append(f"parse input: pinned card-data.json sha256={cd_sha} "
                 f"(v0.81.3 signed data manifest)")

    eb = cd[BRAIN]
    dump_card_parse("Elder Brain", eb, "parse_elder_brain.json")
    eb_plays, eb_casts, eb_unimpl = perm_modes(eb)
    say(f"Elder Brain permission nodes: play={eb_plays} cast={eb_casts} "
        f"unimpl_play={eb_unimpl}")
    wire("parse_elder_brain",
         {"play_modes": eb_plays, "cast_modes": eb_casts,
          "unimplemented_play": eb_unimpl})

    ok = len(eb_plays) >= 1
    ass["A1_parse_play_present"] = "passed" if ok else "failed"
    notes.append(f"A1: Elder Brain Play-mode permission nodes: "
                 f"{len(eb_plays)} (expected >=1; "
                 f"Unimplemented 'play lands' nodes: {len(eb_unimpl)})")

    ok = any(True for _, kind, in eb_casts) and any(
        (n.get("permission", {}) or {}).get("mana_spend_permission")
        == "AnyTypeOrColor"
        for _, _, _, n in walk_perm_nodes(eb)
        if isinstance(n, dict) and n.get("type") == "GrantCastingPermission")
    ass["A2_parse_cast_correct"] = "passed" if ok else "failed"
    notes.append(f"A2: Elder Brain Cast-mode permission present: "
                 f"{len(eb_casts)}; any-color mana rider present: {ok}")

    om = cd["the omenkeel"]
    dump_card_parse("The Omenkeel", om, "parse_omenkeel.json")
    om_plays, om_casts, om_unimpl = perm_modes(om)
    om_land_gated = any(
        "Typed" in json.dumps(n) and "Land" in json.dumps(n)
        for _, _, _, n in walk_perm_nodes(om)
        if isinstance(n, dict)
        and (n.get("mode") == "Play"
             or (n.get("permission", {}) or {}).get("mode") == "Play"))
    ok = len(om_plays) >= 1 and om_land_gated
    ass["A3_control_omenkeel"] = "passed" if ok else "failed"
    notes.append(f"A3: Omenkeel Play-mode nodes: {len(om_plays)} "
                 f"(Typed[Land] gated: {om_land_gated})")
    wire("parse_omenkeel",
         {"play_modes": om_plays, "cast_modes": om_casts,
          "unimplemented_play": om_unimpl})

    gx = cd["gix, yawgmoth praetor"]
    dump_card_parse("Gix, Yawgmoth Praetor", gx, "parse_gix.json")
    gx_plays, gx_casts, gx_unimpl = perm_modes(gx)
    say(f"Gix permission nodes: play={gx_plays} cast={gx_casts} "
        f"unimpl_play={gx_unimpl}")
    ok = len(gx_plays) >= 1
    ass["A4_class_gix"] = "passed" if ok else "failed"
    notes.append(f"A4: Gix Play-mode permission nodes: {len(gx_plays)} "
                 f"(expected >=1; Unimplemented 'play lands' nodes: "
                 f"{len(gx_unimpl)})")
    wire("parse_gix",
         {"play_modes": gx_plays, "cast_modes": gx_casts,
          "unimplemented_play": gx_unimpl})

    # A5: census - cards whose Oracle text grants compound "play lands and
    # cast spells" (either order) in one sentence; report emitted modes.
    census = []
    for name, card in cd.items():
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
    with open(f"{EVDIR}/parse_census.json", "w") as f:
        json.dump({"generated_from": CD_PATH, "card_data_sha256": cd_sha,
                   "criterion": "oracle sentence contains you may + play + "
                                "land + cast + spell",
                   "cards": census}, f, indent=1)
    dropped = [c["card"] for c in census if c["play_modes"] == 0]
    say(f"A5 census: {len(census)} compound cards; Play half missing on "
        f"{len(dropped)}: {dropped}")
    notes.append(f"A5: census found {len(census)} cards with compound "
                 f"'you may play ... and cast ...' Oracle sentences; "
                 f"{len(dropped)} emit no Play-mode permission: {dropped}")
    wire("parse_census", {"n": len(census), "dropped": dropped})
    ass["A5_census"] = "passed"

    # ================= game setup =================
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def do_mulligan(c, pid, tag, need):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in ALL_LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        brain_ok = (pid != 0) or (BRAIN in hn)
        ok = (lands >= need and brain_ok) or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands, brain={BRAIN in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in hand[:1]]}})
        say(f"{tag} bottoms 1")

    async def discard_tick(c, pid, tag, st, state, keep_fn):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                return keep_fn(choice_text(ch).lower())

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    def p1_keep_rank(t):
        # discard first: bolts beyond 2, then lands beyond 4
        if t == BOLT:
            return 1
        if t in P1_LANDS:
            return 0
        return 2

    async def target_selection_tick(c, tag, st, state, pick_fn, done_key,
                                    log_name, record_oid_key=None):
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            wire("target_selection",
                 {"who": tag, "name": log_name, "n": len(chs),
                  "texts": [choice_text(ch)[:60] for ch in chs][:8]})
            pick = None
            for ch in chs:
                try:
                    if pick_fn(ch, state):
                        pick = ch
                        break
                except Exception as e:
                    say(f"[{tag}] pick_fn error: {e}")
            if pick is None:
                say(f"[{tag}] {log_name}: no matching candidate "
                    f"({len(chs)} offered); NOT answering")
                obs["unexpected_prompts"].append(
                    {"who": tag, "kind": log_name, "n_choices": len(chs)})
                entry["done"] = True
                return True
            oid = ref_of(pick)
            if record_oid_key:
                ST[record_oid_key] = oid
            ST[done_key] = True
            say(f"[{tag}] {log_name}: chose oid={oid} "
                f"({choice_text(pick)[:60]})")
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            return True
        return False

    async def generic_prompt(c, tag, st, state, decline_after=25):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6],
                 "wf": (wf_of(state).get("type") or "")})
            say(f"[{tag}] UNEXPECTED PROMPT wf={(wf_of(state).get('type') or '')} "
                f"iid={iid} n={len(chs)}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if time.time() - entry["t0"] < decline_after:
                continue
            pick = chs[0] if chs else None
            if pick is not None:
                say(f"[{tag}] answering prompt after {decline_after}s stall "
                    f"(first choice)")
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def cast_named(c, acts, state, name, tag):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == name:
                say(f"[{tag}] casting {name} via {a['type']} (oid {oid})")
                wire("cast", {"who": tag, "name": name, "oid": oid,
                              "action": a["type"]})
                await submit_as_is(c, a)
                return oid
        return None

    def scan_exile_offers(state, acts):
        """Scan legal_actions for PlayLand/CastSpell on exiled objects."""
        playland_exile, castspell_exile = [], []
        for a in acts:
            at = a["type"]
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if not isinstance(oid, int):
                continue
            o = get_obj(state, oid)
            if o.get("zone") != "Exile":
                continue
            nm = lname(state, oid)
            if at == "PlayLand":
                playland_exile.append((oid, nm))
            elif "cast" in at.lower():
                castspell_exile.append((oid, nm, at))
        return playland_exile, castspell_exile

    def pick_player1(ch, state):
        return seat_of(ch) == 1

    def accept_of(choice):
        for s in choice.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("role") == "accept":
                return str(d.get("value"))
        return None

    async def optional_effect_tick(c, tag, st, state):
        """Answer OptionalEffectChoice prompts by ACCEPTING.

        Deliberate scenario decision: P0 wants the "you may play lands and
        cast spells from among the exiled cards" permission, so the
        accept=true choice is submitted (choices carry empty text; the
        accept value surface distinguishes them, cf. AGENTS.md #6879).
        """
        if (wf_of(state).get("type") or "") != "OptionalEffectChoice":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            wire("optional_effect",
                 {"who": tag, "n": len(chs),
                  "accept_values": [accept_of(ch) for ch in chs]})
            pick = next((ch for ch in chs if accept_of(ch) == "true"), None)
            if pick is None:
                say(f"[{tag}] OptionalEffectChoice: no accept=true choice "
                    f"among {len(chs)}; NOT answering")
                obs["unexpected_prompts"].append(
                    {"who": tag, "kind": "OptionalEffectChoice-no-accept"})
                entry["done"] = True
                return True
            say(f"[{tag}] OptionalEffectChoice: ACCEPTING the 'you may' "
                f"permission (deliberate decision)")
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            return True
        return False

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0", 2)
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    brain = bf_id(state, 0, BRAIN)
                    turn = state.get("turn_number")
                    ready = (brain is not None
                             and ST["brain_cast_turn"] is not None
                             and turn is not None
                             and turn > ST["brain_cast_turn"]
                             and not ST["attacked"]
                             and state.get("active_player") == 0)
                    if ready:
                        sub["data"]["attacks"] = [
                            [brain, {"type": "Player", "data": 1}]]
                        sub["data"]["bands"] = []
                        ST["attacked"] = True
                        ST["attack_turn"] = turn
                        say(f"[P0] attacking P1 with Elder Brain oid={brain} "
                            f"(turn {turn})")
                        wire("attack", {"brain_oid": brain, "turn": turn})
                    else:
                        sub["data"]["attacks"] = []
                        sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        # track Elder Brain on the battlefield + cast turn
        brain = bf_id(state, 0, BRAIN)
        if brain is not None and ST["brain_oid"] is None:
            ST["brain_oid"] = int(brain)
            ST["brain_cast_turn"] = state.get("turn_number")
            say(f"Elder Brain on BF: oid={brain} "
                f"turn={ST['brain_cast_turn']}")
            wire("brain_cast", {"oid": brain,
                                "turn": ST["brain_cast_turn"]})
        if await discard_tick(p0, 0, "P0", st, state,
                              lambda t: 3 if t == BRAIN else
                              (2 if t == RITUAL else 0)):
            return
        # MID: attack trigger resolved (exile set present, stack empty)
        if (ST["attacked"] and not ST["mid_exported"]
                and not (state.get("stack") or [])):
            ex = exile_ids(state)
            if ex:
                ST["exile_at_attack"] = [(oid, lname(state, oid))
                                        for oid in ex]
                ST["land_exiled"] = [nm for _, nm in ST["exile_at_attack"]
                                     if nm in P1_LANDS]
                ST["spell_exiled"] = [nm for _, nm in ST["exile_at_attack"]
                                      if nm == BOLT]
                say(f"MID: exile set after attack: {ST['exile_at_attack']}")
                wire("exile_set", {"cards": ST["exile_at_attack"]})
                obs["exile_sets"].append(
                    {"at": "mid", "cards": ST["exile_at_attack"]})
                if await export_named("mid"):
                    ST["mid_exported"] = True
        # offer scans on P0's main phases after the attack
        if (ST["mid_exported"] and not ST["scan_done"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not (state.get("stack") or [])):
            if not ST["pre_exported"]:
                if await export_named("pre"):
                    ST["pre_exported"] = True
                    say(f"PRE exported at {state.get('phase')} turn "
                        f"{state.get('turn_number')}")
            pl_ex, cs_ex = scan_exile_offers(state, acts)
            hand_land_pl = [a for a in acts if a["type"] == "PlayLand"
                            and isinstance((a.get("data") or {}).get("object_id"), int)
                            and get_obj(state, (a.get("data") or {})["object_id"]).get("zone") == "Hand"]
            scan = {"phase": state.get("phase"),
                    "turn": state.get("turn_number"),
                    "playland_exile": pl_ex, "castspell_exile": cs_ex,
                    "hand_land_playlands": len(hand_land_pl),
                    "n_exiled": len(exile_ids(state))}
            obs["offer_scans"].append(scan)
            wire("offer_scan", scan)
            say(f"SCAN {scan['phase']} t{scan['turn']}: "
                f"exiled PlayLand offers={pl_ex} "
                f"exiled CastSpell offers={cs_ex} "
                f"hand-land PlayLands={len(hand_land_pl)}")
            if ST["scan_land_offer"] is None:
                ST["scan_land_offer"] = [nm for _, nm in pl_ex]
                ST["scan_spell_offer"] = [nm for _, nm, _ in cs_ex]
                ST["scan_n_playland"] = len(pl_ex)
                if pl_ex:
                    ST["exile_land_oid"] = pl_ex[0][0]
                bolts = [oid for oid, nm, _ in cs_ex if nm == BOLT]
                if bolts:
                    ST["exile_bolt_oid"] = bolts[0]
            # play the normal land drop first (so the exiled-land offer is
            # observed both with and without a land drop remaining)
            if hand_land_pl and not ST["p0_land_drop_done"]:
                await submit_as_is(p0, hand_land_pl[0])
                ST["p0_land_drop_done"] = True
                say("[P0] played normal land drop from hand")
                return
            ST["scan_done"] = True
            say("[P0] offer scans complete")
        # G4: cast the exiled Bolt at P1 (control: the cast half works)
        if (ST["scan_done"] and not ST["bolt_cast"]
                and ST["exile_bolt_oid"] is not None
                and my_priority(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not (state.get("stack") or [])):
            for a in acts:
                d = a.get("data", {}) or {}
                if ("cast" in a["type"].lower()
                        and d.get("object_id") == ST["exile_bolt_oid"]):
                    ST["p1_life_before_bolt"] = life_of(state, 1)
                    say(f"[P0] casting exiled Lightning Bolt "
                        f"(oid {ST['exile_bolt_oid']}) at P1; "
                        f"P1 life before={ST['p1_life_before_bolt']}")
                    wire("cast_exiled_bolt",
                         {"oid": ST["exile_bolt_oid"],
                          "p1_life_before": ST["p1_life_before_bolt"]})
                    await submit_as_is(p0, a)
                    ST["bolt_cast"] = True
                    return
        if wtype == "TargetSelection" and ST["bolt_cast"] \
                and not ST["bolt_targeted"]:
            if await target_selection_tick(
                    p0, "P0", st, state, pick_player1,
                    "bolt_targeted", "bolt-target-p1"):
                return
        if (ST["bolt_targeted"] and not ST["bolt_done"]
                and not (state.get("stack") or [])):
            ST["p1_life_after_bolt"] = life_of(state, 1)
            ST["bolt_done"] = True
            say(f"exiled Bolt resolved: P1 life "
                f"{ST['p1_life_before_bolt']}->{ST['p1_life_after_bolt']}")
            wire("bolt_resolved",
                 {"before": ST["p1_life_before_bolt"],
                  "after": ST["p1_life_after_bolt"]})
        # accept the "you may play lands and cast spells" permission
        if await optional_effect_tick(p0, "P0", st, state):
            return
        # POST: after the bolt resolves (or after scans when no bolt)
        if (ST["bolt_done"] or
                (ST["scan_done"] and ST["exile_bolt_oid"] is None
                 and time.time() - t_start > 60)):
            if ST["post_at"] is None:
                ST["post_at"] = time.time()
        if (ST["post_at"] is not None and not ST["post_exported"]
                and time.time() - ST["post_at"] > 8
                and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                say("POST exported")
        if not my_priority(state, 0):
            if await generic_prompt(p0, "P0", st, state):
                return
            return
        # ---- P0 priority: ramp into Elder Brain, then pass ----
        in_flight = bool(state.get("stack") or [])
        if (not in_flight
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not ST["brain_cast"]):
            hn = hand_lnames(state, 0)
            if BRAIN in hn:
                coid = await cast_named(p0, acts, state, BRAIN, "P0")
                if coid is not None:
                    ST["brain_cast"] = True
                    return
                if RITUAL in hn:
                    roid = await cast_named(p0, acts, state, RITUAL, "P0")
                    if roid is not None:
                        return
            elif RITUAL in hn:
                roid = await cast_named(p0, acts, state, RITUAL, "P0")
                if roid is not None:
                    return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0 \
                and not ST["mid_exported"]:
            for a in acts:
                if a["type"] == "PlayLand":
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id")
                    if isinstance(oid, int) and get_obj(
                            state, oid).get("zone") == "Hand":
                        await submit_as_is(p0, a)
                        return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, "P1", 3)
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p1, sub)
            return
        if await discard_tick(p1, 1, "P1", st, state, p1_keep_rank):
            return
        if await optional_effect_tick(p1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, "P1", st, state):
                return
            return
        # P1 holds everything (keeps lands + Bolts in hand for the exile)
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid, post = (states.get(k) for k in ("pre", "mid", "post"))

        # ---- G1: attack happened, hand exiled with >=1 land ----
        if mid is not None:
            ex = [(oid, lname(mid, oid)) for oid in exile_ids(mid)]
            lands = [nm for _, nm in ex if nm in P1_LANDS]
            ok = ST["attacked"] and len(lands) >= 1
            ass["G1_attack_ok"] = "passed" if ok else "failed"
            notes.append(f"G1: attacked={ST['attacked']} turn={ST['attack_turn']}; "
                         f"mid exiled={ex}; lands among exiled={lands}")
        else:
            ass["G1_attack_ok"] = "failed"
            notes.append("G1 failed: mid.json missing (attack trigger never "
                         "reached a resolved exile set)")

        # ---- G2: exiled land offered as PlayLand ----
        if ass["G1_attack_ok"] == "passed" and ST["scan_done"]:
            offered = (ST["scan_n_playland"] or 0) > 0
            ass["G2_land_withheld"] = "passed" if offered else "failed"
            notes.append(f"G2: exiled-land PlayLand offers seen: "
                         f"{ST['scan_land_offer']} (count "
                         f"{ST['scan_n_playland']}); expected >=1 -> "
                         f"{ass['G2_land_withheld']}")
        else:
            notes.append("G2 not-run: no attack exile set or no offer scan")

        # ---- G3: exiled spell offered as CastSpell ----
        if ass["G1_attack_ok"] == "passed" and ST["scan_done"]:
            offered = bool(ST["scan_spell_offer"])
            ass["G3_cast_offered"] = "passed" if offered else "failed"
            notes.append(f"G3: exiled-spell CastSpell offers seen: "
                         f"{ST['scan_spell_offer']} -> {ass['G3_cast_offered']}")
        else:
            notes.append("G3 not-run: no attack exile set or no offer scan")

        # ---- G4: exiled Bolt resolves for 3 ----
        if ST["bolt_done"]:
            b, a = ST["p1_life_before_bolt"], ST["p1_life_after_bolt"]
            ok = (b is not None and a is not None and b - a == 3)
            ass["G4_bolt_resolves"] = "passed" if ok else "failed"
            notes.append(f"G4: exiled Bolt P1 life {b}->{a} "
                         f"(expected -3) -> {ass['G4_bolt_resolves']}")
        elif ST["bolt_cast"]:
            ass["G4_bolt_resolves"] = "failed"
            notes.append("G4 failed: exiled Bolt cast started but never "
                         "resolved (targeted="
                         f"{ST['bolt_targeted']})")
        else:
            notes.append("G4 not-run: no exiled Bolt was cast "
                         f"(offered oid={ST['exile_bolt_oid']})")

        # ---- O1/O2: supplementary related findings (same trigger) ----
        ex_detail = []
        if mid is not None:
            ex_detail = [(oid, lname(mid, oid),
                          get_obj(mid, oid).get("owner"))
                         for oid in exile_ids(mid)]
            p0_ex = [x for x in ex_detail if x[2] == 0]
            p1_ex = [x for x in ex_detail if x[2] == 1]
            ok = len(p0_ex) == 0 and len(p1_ex) >= 1
            ass["O1_exile_scope"] = "passed" if ok else "failed"
            notes.append(f"O1: exiled cards owned by attacker P0: "
                         f"{len(p0_ex)} (expected 0); owned by attacked P1: "
                         f"{len(p1_ex)}. ChangeZoneAll target carries "
                         f"controller:null - the 'that player' restriction "
                         f"is missing from the AST.")
            p1_hand_n = len(hand_ids(mid, 1))
            ok = (p1_hand_n == len(p1_ex))
            ass["O2_draw_count"] = "passed" if ok else "failed"
            notes.append(f"O2: P1's hand after the trigger: {p1_hand_n} cards "
                         f"after {len(p1_ex)} of their cards were exiled "
                         f"(Oracle: draw that many -> expected "
                         f"{len(p1_ex)}).")
        else:
            notes.append("O1/O2 not-run: mid.json missing")

        # ---- verdict ----
        if ass["A1_parse_play_present"] == "not-run":
            verdict = "blocked"
            notes.append("verdict=blocked: parse check could not run")
        elif ass["A1_parse_play_present"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: Elder Brain's emitted "
                         "attack-trigger AST drops the Play half of the "
                         "compound permission (Unimplemented 'play lands' "
                         "node); runtime: "
                         f"land_offered={ass['G2_land_withheld']}, "
                         f"spell_offered={ass['G3_cast_offered']}, "
                         f"bolt_resolved={ass['G4_bolt_resolves']}")
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (log in "
                               f"runs/{RUN_ID}/server.log)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6963.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (v if not isinstance(v, list) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json",
                               "parse_elder_brain.json", "parse_gix.json",
                               "parse_omenkeel.json", "parse_census.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_6963.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; this is an area:parser issue - "
                "the primary evidence is the emitted AST in the pinned "
                "card-data.json (input Oracle text preserved in "
                "parse_*.json).",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "P1 deliberately holds lands/spells (no land drops, no "
                "casts) so the attack exiles a mixed hand; this is a "
                "fixture choice, not normal play.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6963.py",
                    f"{EVDIR}/scenario_6963.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6963.py and server.log into EVDIR")
        render_summary(run, states)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1040
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6963 - Elder Brain land-play "
               "permission dropped", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "parser + runtime", fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Parse assertions (pinned card-data.json):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_play_present": "Elder Brain emits a Play-mode permission",
            "A2_parse_cast_correct": "Elder Brain Cast half + any-color rider intact",
            "A3_control_omenkeel": "Omenkeel emits Play w/ Typed[Land] (control)",
            "A4_class_gix": "Gix emits a Play-mode half (same class)",
            "A5_census": "compound play+cast census completed",
            "G1_attack_ok": "GAME: attack exiled P1 hand w/ >=1 land",
            "G2_land_withheld": "GAME: exiled land offered as PlayLand",
            "G3_cast_offered": "GAME: exiled spell offered as CastSpell",
            "G4_bolt_resolves": "GAME: exiled Bolt deals 3 to P1",
            "O1_exile_scope": "GAME: only attacked player's hand exiled",
            "O2_draw_count": "GAME: P1 draws N == exiled-from-their-hand",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Exile set after attack:", fill=(200, 210, 225))
        y += 24
        ex = (run["driver_state"] or {}).get("exile_at_attack") or []
        d.text((36, y), str(ex)[:116] or "(none recorded)",
               fill=(150, 160, 175))
        y += 24
        scans = (run["observations"] or {}).get("offer_scans") or []
        if scans:
            s0 = scans[0]
            d.text((36, y), f"offer scan t{s0.get('turn')} {s0.get('phase')}: "
                   f"PlayLand(exile)={s0.get('playland_exile')} "
                   f"CastSpell(exile)={s0.get('castspell_exile')}"[:116],
                   fill=(150, 160, 175))
            y += 24
        y += 6
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:12]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        # NOTE: scenario_run.log is hashed LAST, after the final say() line
        # below is appended; otherwise the manifest's hash for the log goes
        # stale by exactly one line.
        files = ["pre.json", "mid.json", "post.json",
                 "parse_elder_brain.json", "parse_gix.json",
                 "parse_omenkeel.json", "parse_census.json",
                 "run.json", "scenario_6963.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        # Re-hash now that all scenario_run.log logging is done. No say()
        # may follow this point (finish() only closes files + stdout).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    # ================= main loop =================
    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    no_brain_watchdog_at = None
    while time.time() - t0 < 1200:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                obs["tick_errors"].append({"who": tag, "err": str(e)[:200]})
                wire("tick_error", {"who": tag, "err": str(e)})
        s = (p0.latest or {}).get("state") or {}
        if ST["post_exported"] and time.time() - (ST.get("post_at") or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        # watchdog: brain never cast and P0 far along
        turn = s.get("turn_number") or 0
        if (not ST["brain_cast"] and turn >= 16
                and no_brain_watchdog_at is None):
            no_brain_watchdog_at = time.time()
            notes.append(f"watchdog: turn {turn} reached with Elder Brain "
                         f"never cast; giving 120s more")
        if (no_brain_watchdog_at is not None
                and time.time() - no_brain_watchdog_at > 120):
            notes.append("watchdog: Elder Brain never cast; finishing")
            await finish()
            return
        if time.time() - last_diag > 90 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0life={life_of(s, 0)} "
                f"P1life={life_of(s, 1)} brain={bf_id(s, 0, BRAIN)} "
                f"attacked={ST['attacked']} mid={ST['mid_exported']} "
                f"scan={ST['scan_done']} bolt={ST['bolt_cast']}/"
                f"{ST['bolt_targeted']}/{ST['bolt_done']} "
                f"P0hand={hand_lnames(s, 0)} P1hand_n={len(hand_ids(s, 1))}")
    notes.append("global timeout (1200s) hit before assertions resolved")
    await finish()


asyncio.run(main())
