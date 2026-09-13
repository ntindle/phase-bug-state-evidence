#!/usr/bin/env python3
"""Issue #6986: Rakdos's per-creature coin-flip iteration is swallowed.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3, sha c1bdd903...):
  Rakdos, the Showstopper ({4}{B}{R}, Creature - Demon 6/6):
    "Flying, trample
     When Rakdos enters, flip a coin for each creature that isn't a Demon,
     Devil, or Imp. Destroy each creature whose coin comes up tails."

Reported symptom (issue body + classifier): the emitted ETB trigger AST
collapses the per-creature coin-flip operation into one generic FlipCoin
followed by an unqualified DestroyAll. A Swallow:DynamicQty parse warning
covers the full ETB operation. No runtime behavior was tested in the
report.

Parse state on v0.81.3 (observed 2026-09-13 before the run):
  triggers[0] (mode ChangesZone, destination Battlefield) =
    execute: Spell/FlipCoin{win_effect:null, lose_effect:null}
      sub_ability: Spell/DestroyAll{target:Typed[Creature],controller:null,
                   cant_regenerate:false} (sub_link SequentialSibling)
  parse_warnings: [{type:SwallowedClause, detector:DynamicQty,
                    unit_span:{first_line:0,last_line:1,...}}]
  The AST drops (a) per-creature iteration, (b) the Demon/Devil/Imp
  subtype exclusions, (c) any creature<->flip-result association.

This is an area:parser issue (classifier:supported-aspect-defect), but the
report's EXPECTED section describes resolution behavior, so the game leg
tests whether the runtime manifests the collapsed AST: one generic flip,
no exclusions honored (Rakdos itself is a Demon and must survive its own
ETB per Oracle), destruction uncorrelated with flip results.

Setup (native engine, two human-client seats, default Bo1):
  P0: 3x Rakdos the Showstopper, 4x Grizzly Bears, 10x Mountain,
      10x Forest, 10x Swamp.
  P1: 24x Mountain, 24x Forest (passive: land drop, pass priority).
  P0 ramps, puts >=2 Bears on the battlefield, casts Rakdos ({4}{B}{R} via
  engine auto-tap). The ETB trigger fires; the driver records every
  coin-flip prompt and deliberately answers HEADS (heads = no tails ->
  per Oracle zero creatures should be destroyed).

Assertions (correct-behavior properties; "failed" = the defect is present):
  Parse (primary; measured on the pinned v0.81.3 card-data.json):
    A1_parse_warn_absent   the ETB carries no Swallow/DynamicQty warning.
                           expected FAIL (the reported gap).
    A2_parse_per_creature  the AST expresses per-creature flips with the
                           Demon/Devil/Imp exclusions.
                           expected FAIL (single FlipCoin + unqualified
                           DestroyAll).
  Game (runtime consequence of the parse defect):
    A3_setup_ok            Rakdos cast with >=2 other creatures on BF and
                           its ETB trigger observed. expected PASS.
    A4_flip_per_creature   one flip prompt per eligible creature
                           (>=2 flips observed). expected FAIL (<=1).
    A5_exclusions_honored  Rakdos (a Demon, excluded by Oracle) survives
                           its own ETB. expected FAIL (destroyed by the
                           unqualified DestroyAll).
    A6_heads_no_destroy    with the single flip answered heads, zero
                           creatures are destroyed. expected FAIL (all
                           destroyed: no creature<->result association).
    A7_cleanup             stack empty, game proceeds. expected PASS.

Deliberate driver decision: the coin flip is answered HEADS whenever a
flip prompt is offered (heads = no tails -> per Oracle zero creatures
should be destroyed). If the FlipCoin resolves silently with no prompt,
the destruction is flip-independent by construction and A6 is recorded
not-run.

Verdict rule: reproduced iff (A1 or A2 fails) AND (A4, A5, or A6 fails).
blocked iff the game never reaches the ETB (A3 not-run/failed). A parse
defect with no reachable runtime trigger is still a valid parse-level
result, but the playbook prefers testing the reported outcome, so the
runtime leg is required for a reproduced verdict here.

Evidence: evidence/6986/<run-id>/pre.json, mid.json, post.json,
parse_rakdos.json, run.json, manifest.sha256, summary.png,
scenario_6986.py, wire_log.jsonl, scenario_run.log, server.log
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
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6986b")
ISSUE = 6986
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RAKDOS = "rakdos, the showstopper"
BEARS = "grizzly bears"
MOUNTAIN = "mountain"
FOREST = "forest"
SWAMP = "swamp"
ALL_LANDS = (MOUNTAIN, FOREST, SWAMP)

P0_DECK = [(RAKDOS, 3), (BEARS, 4), (MOUNTAIN, 10), (FOREST, 10), (SWAMP, 10)]
P1_DECK = [(MOUNTAIN, 24), (FOREST, 24)]

CD_PATH = f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json"

# populated in main() after card-data loads (state-view objects carry no
# usable card_type; names are matched against the pinned card data)
CREATURE_NAMES = set()
SUBTYPE_OF = {}

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


def all_bf_creatures(state):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("base_name") or o.get("name") or "").lower()
            in CREATURE_NAMES]


def flip_eligible(state):
    """Creatures that per Oracle need a coin flip (not Demon/Devil/Imp)."""
    out = []
    for oid in all_bf_creatures(state):
        nm = str(get_obj(state, oid).get("base_name")
                 or get_obj(state, oid).get("name") or "").lower()
        subs = {s.lower() for s in SUBTYPE_OF.get(nm, set())}
        if not (subs & {"demon", "devil", "imp"}):
            out.append(int(oid))
    return out


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


def stack_entries(state):
    return state.get("stack") or []


def rakdos_trigger_on_stack(state):
    """True if a TriggeredAbility sourced from Rakdos is on the stack."""
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        src = e.get("source_id") or e.get("source")
        kind = (e.get("kind") or {})
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if isinstance(src, int) and lname(state, src) == RAKDOS \
                and ktype == "TriggeredAbility":
            return True
        desc = str(e.get("description") or kind.get("description") or "")
        if "rakdos" in desc.lower() and ktype == "TriggeredAbility":
            return True
    return False


def is_flip_like(opp, wf_type):
    blob = json.dumps(opp, default=str).lower()
    wt = (wf_type or "").lower()
    return ("coin" in blob or "flip" in blob or "heads" in blob
            or "tails" in blob or "coin" in wt or "flip" in wt)


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


def pick_heads(chs):
    for ch in chs:
        if "heads" in choice_text(ch).lower():
            return ch
    return chs[0] if chs else None


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_warn_absent", "A2_parse_per_creature",
            "A3_setup_ok", "A4_flip_per_creature", "A5_exclusions_honored",
            "A6_heads_no_destroy", "A7_cleanup")}
    obs = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
           "flip_prompts": [], "etb_window": []}
    ST = {"rakdos_cast": False, "rakdos_cast_turn": None, "rakdos_oid": None,
          "etb_seen": False, "etb_resolved": False,
          "pre_exported": False, "mid_exported": False, "post_exported": False,
          "post_at": None, "flip_count": 0, "flip_answered_heads": 0,
          "pre_board": [], "mid_board": [], "post_board": [],
          "eligible_at_pre": 0}
    prompt_first_seen = {}
    last_select = {}
    kept = {}

    # ================= parse checks (primary evidence) =================
    cd = json.load(open(CD_PATH))
    cd_sha = hashlib.sha256(open(CD_PATH, "rb").read()).hexdigest()
    say(f"card-data.json sha256={cd_sha}")
    notes.append(f"parse input: pinned card-data.json sha256={cd_sha} "
                 f"(v0.81.3 signed data manifest)")
    global CREATURE_NAMES, SUBTYPE_OF
    CREATURE_NAMES = {name for name, c in cd.items()
                      if "Creature" in ((c.get("card_type") or {})
                                        .get("core_types") or [])}
    SUBTYPE_OF = {name: set(((c.get("card_type") or {}).get("subtypes") or []))
                  for name, c in cd.items()}
    say(f"creature names in card-data: {len(CREATURE_NAMES)}")

    rk = cd[RAKDOS]
    with open(f"{EVDIR}/parse_rakdos.json", "w") as f:
        json.dump({"card": "Rakdos, the Showstopper",
                   "oracle_text": rk.get("oracle_text"),
                   "keywords": rk.get("keywords"),
                   "abilities": rk.get("abilities"),
                   "triggers": rk.get("triggers"),
                   "parse_warnings": rk.get("parse_warnings")}, f, indent=1)
    say("saved parse_rakdos.json")

    warns = rk.get("parse_warnings") or []
    swallow_dyn = [w for w in warns
                   if w.get("type") == "SwallowedClause"
                   and w.get("detector") == "DynamicQty"]
    say(f"parse_warnings: {[(w.get('type'), w.get('detector')) for w in warns]}")
    wire("parse_warnings", {"all": [(w.get("type"), w.get("detector"),
                                     w.get("unit_span")) for w in warns]})
    ok = len(swallow_dyn) == 0
    ass["A1_parse_warn_absent"] = "passed" if ok else "failed"
    notes.append(f"A1: Swallow:DynamicQty warnings on the ETB: "
                 f"{len(swallow_dyn)} (expected 0) -> {ass['A1_parse_warn_absent']}")

    # A2: per-creature flip structure + exclusions in the emitted AST.
    # Structural check only: description/oracle strings are stripped first
    # (they carry "for each ... Demon, Devil, or Imp" prose that a naive
    # substring search would mistake for AST structure).
    def strip_desc(node):
        if isinstance(node, dict):
            return {k: strip_desc(v) for k, v in node.items()
                    if k not in ("description", "oracle_text", "name",
                                 "flavor_name")}
        if isinstance(node, list):
            return [strip_desc(v) for v in node]
        return node

    trig = (rk.get("triggers") or [None])[0] or {}
    trig_json = json.dumps(strip_desc(trig))
    flip_count_ast = trig_json.count('"FlipCoin"')
    exec_eff = ((trig.get("execute") or {}).get("effect") or {})
    sub_eff = (((trig.get("execute") or {}).get("sub_ability") or {})
               .get("effect") or {})
    single_flip_null = (exec_eff.get("type") == "FlipCoin"
                        and exec_eff.get("win_effect") is None
                        and exec_eff.get("lose_effect") is None)
    tgt = sub_eff.get("target") or {}
    destroy_unqualified = (
        sub_eff.get("type") == "DestroyAll"
        and tgt.get("type") == "Typed"
        and [str(x) for x in (tgt.get("type_filters") or [])] == ["Creature"]
        and tgt.get("controller") is None)
    exclusion_terms = any(t in trig_json for t in ("Demon", "Devil", "Imp"))
    collapsed = (flip_count_ast == 1 and single_flip_null
                 and destroy_unqualified and not exclusion_terms)
    say(f"AST: FlipCoin nodes={flip_count_ast} single_flip_null="
        f"{single_flip_null} destroy_unqualified={destroy_unqualified} "
        f"exclusion_terms={exclusion_terms} collapsed={collapsed}")
    wire("parse_ast", {"flipcoin_nodes": flip_count_ast,
                       "single_flip_null": single_flip_null,
                       "destroy_unqualified": destroy_unqualified,
                       "exclusion_terms": exclusion_terms,
                       "collapsed": collapsed,
                       "trigger_desc": trig.get("description")})
    ok = not collapsed
    ass["A2_parse_per_creature"] = "passed" if ok else "failed"
    notes.append(f"A2: emitted ETB AST expresses per-creature flips with the "
                 f"Demon/Devil/Imp exclusions: {ok} (FlipCoin nodes="
                 f"{flip_count_ast}, single flip with null win/lose="
                 f"{single_flip_null}, unqualified DestroyAll="
                 f"{destroy_unqualified}, exclusion terms in AST="
                 f"{exclusion_terms}) -> {ass['A2_parse_per_creature']}")

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

    def board_snapshot(state):
        return sorted([(int(oid), lname(state, oid),
                        get_obj(state, oid).get("controller"))
                       for oid in all_bf_creatures(state)])

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in ALL_LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        need_rakdos = RAKDOS in hn if pid == 0 else True
        ok = (lands >= 2 and need_rakdos) or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands, rakdos={RAKDOS in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands, "
                f"rakdos={RAKDOS in hn})")

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

    async def flip_prompt_tick(c, tag, st, state):
        """Record + answer coin-flip prompts during the Rakdos ETB window.

        Deliberate decision: always answer HEADS, so any destruction proves
        the result is uncorrelated with the flip (and zero destruction on a
        correct engine proves heads was honored).
        """
        if not ST["etb_seen"] or ST["etb_resolved"]:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wtype = (wf_of(state).get("type") or "")
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            if not is_flip_like(opp, wtype):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            rec = {"who": tag, "iid": str(iid)[:12], "wf": wtype,
                   "n_choices": len(chs),
                   "texts": [choice_text(ch)[:60] for ch in chs][:6]}
            obs["flip_prompts"].append(rec)
            wire("flip_prompt", {"opportunity":
                                 json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] COIN-FLIP prompt #{ST['flip_count'] + 1} "
                f"wf={wtype} choices={[choice_text(ch)[:20] for ch in chs][:4]}")
            ST["flip_count"] += 1
            pick = pick_heads(chs)
            if pick is None:
                say(f"[{tag}] flip prompt with no choices; NOT answering")
                entry["done"] = True
                continue
            if "heads" in choice_text(pick).lower():
                ST["flip_answered_heads"] += 1
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            acted = True
        return acted

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

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0")
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
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        # track Rakdos on the battlefield + cast turn
        rk = bf_id(state, 0, RAKDOS)
        if rk is not None and ST["rakdos_oid"] is None:
            ST["rakdos_oid"] = int(rk)
            ST["rakdos_cast_turn"] = state.get("turn_number")
            say(f"Rakdos on BF: oid={rk} turn={ST['rakdos_cast_turn']}")
            wire("rakdos_cast", {"oid": rk, "turn": ST["rakdos_cast_turn"]})
        # ETB window: trigger on the stack
        if (ST["rakdos_oid"] is not None and not ST["etb_seen"]
                and rakdos_trigger_on_stack(state)):
            ST["etb_seen"] = True
            ST["pre_board"] = board_snapshot(state)
            # eligible = creatures that per Oracle need a coin flip
            # (not Demon/Devil/Imp)
            ST["eligible_at_pre"] = len(flip_eligible(state))
            say(f"ETB SEEN: trigger on stack; board={ST['pre_board']}")
            wire("etb_seen", {"board": ST["pre_board"]})
            if await export_named("pre"):
                ST["pre_exported"] = True
        # flip prompts during the ETB window (answered heads)
        if await flip_prompt_tick(p0, "P0", st, state):
            return
        # ETB resolved: stack had the trigger, now empty
        if (ST["etb_seen"] and not ST["etb_resolved"]
                and not (state.get("stack") or [])):
            ST["etb_resolved"] = True
            ST["mid_board"] = board_snapshot(state)
            say(f"ETB RESOLVED: board now={ST['mid_board']} "
                f"flips_seen={ST['flip_count']} "
                f"heads_answers={ST['flip_answered_heads']}")
            wire("etb_resolved", {"board": ST["mid_board"],
                                  "flips": ST["flip_count"],
                                  "heads_answers": ST["flip_answered_heads"]})
            if await export_named("mid"):
                ST["mid_exported"] = True
        if await discard_tick(p0, 0, "P0", st, state,
                              lambda t: 3 if t == RAKDOS else
                              (2 if t == BEARS else 0)):
            return
        # POST: after ETB resolved and the game proceeds
        if ST["etb_resolved"]:
            if ST["post_at"] is None:
                ST["post_at"] = time.time()
            if (not ST["post_exported"] and ST["post_at"] is not None
                    and time.time() - ST["post_at"] > 10
                    and not (state.get("stack") or [])):
                ST["post_board"] = board_snapshot(state)
                if await export_named("post"):
                    ST["post_exported"] = True
                    say(f"POST exported; board={ST['post_board']}")
        if not my_priority(state, 0):
            if await generic_prompt(p0, "P0", st, state):
                return
            return
        # ---- P0 priority: build the board, then cast Rakdos ----
        in_flight = bool(state.get("stack") or [])
        bears_n = len(bf_ids(state, 0, BEARS))
        if (not in_flight
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            hn = hand_lnames(state, 0)
            untapped = len(untapped_lands(state, 0))
            if not ST["rakdos_cast"] and RAKDOS in hn and untapped >= 6 \
                    and bears_n >= 2 and bf_id(state, 0, RAKDOS) is None:
                coid = await cast_named(p0, acts, state, RAKDOS, "P0")
                if coid is not None:
                    ST["rakdos_cast"] = True
                    say(f"[P0] cast Rakdos with {bears_n} Bears on BF, "
                        f"{untapped} untapped lands")
                    return
            if BEARS in hn and bears_n < 4 and untapped >= 2:
                coid = await cast_named(p0, acts, state, BEARS, "P0")
                if coid is not None:
                    return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0 \
                and not ST["rakdos_cast"]:
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
                await do_mulligan(p1, 1, "P1")
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
        if await discard_tick(p1, 1, "P1", st, state, lambda t: 0):
            return
        if await flip_prompt_tick(p1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, "P1", st, state):
                return
            return
        # P1 passive: land drop, then pass
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 1:
            for a in acts:
                if a["type"] == "PlayLand":
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id")
                    if isinstance(oid, int) and get_obj(
                            state, oid).get("zone") == "Hand":
                        await submit_as_is(p1, a)
                        return
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

        # ---- A3: setup reached the ETB ----
        if pre is not None and ST["etb_seen"]:
            pre_board = board_snapshot(pre)
            bears_pre = [x for x in pre_board if x[1] == BEARS]
            rak_pre = [x for x in pre_board if x[1] == RAKDOS]
            ok = len(bears_pre) >= 2 and len(rak_pre) == 1
            ass["A3_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A3: pre.json board={pre_board}; bears={len(bears_pre)} "
                         f"(need >=2), rakdos={len(rak_pre)} -> "
                         f"{ass['A3_setup_ok']}")
        else:
            ass["A3_setup_ok"] = "failed"
            notes.append("A3 failed: ETB never observed "
                         f"(rakdos_cast={ST['rakdos_cast']}, "
                         f"etb_seen={ST['etb_seen']})")

        # ---- A4: one flip per eligible creature ----
        if ass["A3_setup_ok"] == "passed":
            eligible = ST["eligible_at_pre"]
            flips = ST["flip_count"]
            ok = flips >= eligible and flips > 1
            ass["A4_flip_per_creature"] = "passed" if ok else "failed"
            notes.append(f"A4: eligible creatures at pre={eligible}; "
                         f"coin-flip prompts observed={flips} "
                         f"(heads answers={ST['flip_answered_heads']}); "
                         f"per-creature iteration present: {ok} -> "
                         f"{ass['A4_flip_per_creature']}")
        else:
            notes.append("A4 not-run: setup never reached the ETB")

        # ---- A5: the excluded Demon survives its own ETB ----
        if ass["A3_setup_ok"] == "passed" and mid is not None:
            mid_board = board_snapshot(mid)
            rak_mid = [x for x in mid_board if x[1] == RAKDOS]
            ok = len(rak_mid) == 1
            ass["A5_exclusions_honored"] = "passed" if ok else "failed"
            notes.append(f"A5: mid.json board={mid_board}; Rakdos (a Demon, "
                         f"excluded by Oracle) on BF: {len(rak_mid)} "
                         f"(expected 1) -> {ass['A5_exclusions_honored']}")
        else:
            notes.append("A5 not-run: no mid.json (ETB never resolved)")

        # ---- A6: heads flip -> zero destruction ----
        if (ass["A3_setup_ok"] == "passed" and mid is not None
                and ST["flip_answered_heads"] >= 1):
            mid_board = board_snapshot(mid)
            pre_board = board_snapshot(pre) if pre is not None else []
            destroyed = len(pre_board) - len(mid_board)
            ok = destroyed == 0
            ass["A6_heads_no_destroy"] = "passed" if ok else "failed"
            notes.append(f"A6: answered heads {ST['flip_answered_heads']}x; "
                         f"creatures pre={len(pre_board)} mid={len(mid_board)}; "
                         f"destroyed={destroyed} (expected 0 on heads) -> "
                         f"{ass['A6_heads_no_destroy']}")
        elif ass["A3_setup_ok"] == "passed" and mid is not None:
            notes.append(f"A6 not-run: no flip prompt was ever answered "
                         f"(flips observed={ST['flip_count']}); "
                         f"destruction was flip-independent by construction")
        else:
            notes.append("A6 not-run: no ETB resolution observed")

        # ---- A7: cleanup ----
        if post is not None:
            ok = not (post.get("stack") or [])
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: post.json stack empty={ok}, "
                         f"turn={post.get('turn_number')}, "
                         f"phase={post.get('phase')} -> {ass['A7_cleanup']}")
        else:
            notes.append("A7 not-run: post.json missing")

        # ---- verdict ----
        parse_fail = ass["A1_parse_warn_absent"] == "failed" \
            or ass["A2_parse_per_creature"] == "failed"
        runtime_fail = ass["A4_flip_per_creature"] == "failed" \
            or ass["A5_exclusions_honored"] == "failed" \
            or ass["A6_heads_no_destroy"] == "failed"
        if ass["A3_setup_ok"] in ("not-run",) or (
                ass["A3_setup_ok"] == "failed"
                and not ST["etb_seen"]):
            verdict = "blocked"
            notes.append("verdict=blocked: the game never reached Rakdos's "
                         "ETB trigger")
        elif parse_fail and runtime_fail:
            verdict = "reproduced"
            notes.append("verdict=reproduced: parse defect (Swallow:DynamicQty "
                         "+ collapsed single-FlipCoin/unqualified-DestroyAll "
                         "AST) manifests at runtime: "
                         f"flips={ST['flip_count']} heads_answers="
                         f"{ST['flip_answered_heads']} "
                         f"pre_board={ST['pre_board']} mid_board="
                         f"{ST['mid_board']}")
        elif (ass["A1_parse_warn_absent"] == "passed"
                and ass["A2_parse_per_creature"] == "passed"
                and ass["A4_flip_per_creature"] == "passed"
                and ass["A5_exclusions_honored"] == "passed"
                and (ass["A6_heads_no_destroy"] in ("passed", "not-run"))
                and ass["A7_cleanup"] == "passed"):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete/mixed assertion chain")
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
                open(f"{BACKFILL}/driver/scenario_6986.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (v if not isinstance(v, list) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json",
                               "parse_rakdos.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_6986.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; this is an area:parser issue - "
                "the primary evidence is the emitted AST in the pinned "
                "card-data.json (input Oracle text preserved in "
                "parse_rakdos.json).",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Rakdos itself (a Demon) is the exclusion representative; "
                "Devil/Imp subtypes were not separately fielded (same "
                "exclusion clause, same absent AST filter).",
                "The coin flip was deliberately answered HEADS every time; "
                "the tails branch was not exercised.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6986.py",
                    f"{EVDIR}/scenario_6986.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6986.py and server.log into EVDIR")
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
        W, H = 1000, 1020
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6986 - Rakdos, the Showstopper "
               "per-creature coin-flip iteration swallowed",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "parser + runtime", fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (correct-behavior properties; "
               "failed = defect present):", fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_warn_absent": "PARSE: no Swallow:DynamicQty warning on the ETB",
            "A2_parse_per_creature": "PARSE: AST has per-creature flips + exclusions",
            "A3_setup_ok": "GAME: Rakdos cast, >=2 Bears, ETB observed",
            "A4_flip_per_creature": "GAME: one flip prompt per eligible creature",
            "A5_exclusions_honored": "GAME: Rakdos (Demon, excluded) survives its ETB",
            "A6_heads_no_destroy": "GAME: heads flip -> zero creatures destroyed",
            "A7_cleanup": "GAME: stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Oracle: flip a coin for each creature that isn't a "
               "Demon, Devil, or Imp;", fill=(200, 210, 225))
        y += 24
        d.text((36, y), "destroy each creature whose coin comes up tails. "
               "Flip deliberately answered HEADS.", fill=(150, 160, 175))
        y += 30
        dst = run.get("driver_state") or {}
        d.text((24, y), f"pre board:  {dst.get('pre_board')}",
               fill=(150, 160, 175))
        y += 24
        d.text((24, y), f"mid board:  {dst.get('mid_board')}",
               fill=(150, 160, 175))
        y += 24
        d.text((24, y), f"flips observed: {dst.get('flip_count')} "
               f"(heads answers: {dst.get('flip_answered_heads')})",
               fill=(150, 160, 175))
        y += 30
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
        # NOTE: scenario_run.log is hashed LAST, after all say() logging is
        # done; no say() may follow the final build().
        files = ["pre.json", "mid.json", "post.json",
                 "parse_rakdos.json", "run.json", "scenario_6986.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]

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
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    # ================= main loop =================
    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    no_rakdos_watchdog_at = None
    while time.time() - t0 < 900:
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
        turn = s.get("turn_number") or 0
        if (not ST["rakdos_cast"] and turn >= 14
                and no_rakdos_watchdog_at is None):
            no_rakdos_watchdog_at = time.time()
            notes.append(f"watchdog: turn {turn} reached with Rakdos "
                         f"never cast; giving 90s more")
        if (no_rakdos_watchdog_at is not None
                and time.time() - no_rakdos_watchdog_at > 90):
            notes.append("watchdog: Rakdos never cast; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0life={life_of(s, 0)} "
                f"P1life={life_of(s, 1)} rakdos={bf_id(s, 0, RAKDOS)} "
                f"bears={len(bf_ids(s, 0, BEARS))} "
                f"etb={ST['etb_seen']}/{ST['etb_resolved']} "
                f"flips={ST['flip_count']} "
                f"P0hand={hand_lnames(s, 0)}")
    notes.append("global timeout (900s) hit before assertions resolved")
    await finish()


asyncio.run(main())
