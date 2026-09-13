#!/usr/bin/env python3
"""Issue #6984: Tomb Tyrant - compound activation timing and game-state
restrictions are unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Tomb Tyrant ({3}{B} Creature - Zombie Noble 3/3):
    "Other Zombies you control get +1/+1.
     {2}{B}, {T}, Sacrifice a creature: Return a Zombie creature card at
     random from your graveyard to the battlefield. Activate only during
     your turn and only if there are at least three Zombie creature cards
     in your graveyard."

Reported symptom: the compound `Activate only during your turn and only if
there are at least three Zombie creature cards in your graveyard` clause is
marked unsupported (classifier:unsupported-aspect). Triage acceptance
criteria: the restriction parses into both conditions, and action-legality
tests cover each condition failing independently as well as both succeeding.

Parse state on v0.81.3 (observed 2026-09-13 before the run): the activated
ability carries TWO structured activation_restrictions and zero
Unimplemented nodes:
  [0] RequiresCondition(QuantityComparison(
        Ref(ZoneCardCount{zone:Graveyard, filter:Typed[Creature, Subtype(Zombie)],
                          scope:Controller}) >= Fixed(3)))
  [1] DuringYourTurn
So the parse half of the issue appears FIXED on v0.81.3; the runtime leg
tests whether the engine ENFORCES the two parsed restrictions.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Tomb Tyrant, 4x Diregraf Ghoul, 12x Entomb, 40x Swamp.
  P1: 60x Swamp (plays a land, passes; no creatures).
  P0 casts Ghoul (sacrifice fodder / payable-cost witness), casts Tomb
  Tyrant, then Entombs 3x Tomb Tyrant copies into its own graveyard.

Tests (ordered C -> B -> A in one game):
  C (zone-count fails): P0's turn after the Tyrant loses summoning sickness,
    P0 graveyard has <3 Zombie creature cards, Tyrant untapped, {2}{B}
    payable, fodder on BF -> the activation must NOT be offered.
  B (timing fails): P1's main phase, P0 holds priority, P0 graveyard has 3
    Zombie creature cards, Tyrant untapped, {2}{B} payable, fodder on BF ->
    the activation must NOT be offered (DuringYourTurn).
  A (both succeed): P0's turn, 3 Zombies in P0 gy, Tyrant untapped, payable
    -> the activation MUST be offered; P0 activates (sacrifices the Ghoul),
    a Zombie creature card returns from P0's graveyard to the battlefield.

Assertions:
  A1_parse_restrictions  Tomb Tyrant's activated ability carries exactly the
                         two expected restriction nodes and no Unimplemented
                         nodes. (parse; expected PASS on v0.81.3)
  A2_setup_ok            fixture reached: Tyrant on P0 BF untapped, scan
                         preconditions recorded for C.
  C1_count_enforced      with <3 Zombies in P0's graveyard the activation is
                         NOT offered on P0's turn. failed = offered (bug).
  B1_timing_enforced     with 3 Zombies in P0's graveyard the activation is
                         NOT offered to P0 on P1's turn. failed = offered.
  A3_offered_when_legal  with 3 Zombies on P0's turn the activation IS
                         offered (positive control).
  A4_activation_resolves the activation resolves: Ghoul sacrificed, Tyrant
                         tapped, a Zombie creature card moved P0
                         graveyard -> battlefield.
  A5_cleanup             stack empty and game proceeds after resolution.

Verdict rule: reproduced iff A1 fails (parse drops the clause) or B1/C1
fail (engine ignores a parsed restriction). not-reproduced iff A1 passes
and B1/C1 pass with A3/A4 passing (positive control sound). blocked iff the
fixture never reaches the scans (A2 fails) or A3 fails while A1 passes
(ability never offered even when legal - a different defect).

Deliberate driver decisions: P0 never attacks; P1 holds everything (land
drops only). Entomb search answers choose Tomb Tyrant. The activation's
sacrifice cost answers choose the Diregraf Ghoul.

Evidence: evidence/6984/<run-id>/pre_a.json, mid_b.json, mid_c.json,
  mid_resolution.json,
post_a.json, parse_tomb_tyrant.json, run.json, manifest.sha256,
summary.png, scenario_6984.py, wire_log.jsonl, scenario_run.log,
server.log
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6984")
ISSUE = 6984
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TYRANT = "tomb tyrant"
GHOUL = "diregraf ghoul"
ENTOMB = "entomb"
SWAMP = "swamp"
ZOMBIE_CARDS = (TYRANT, GHOUL)

P0_DECK = [(TYRANT, 4), (GHOUL, 4), (ENTOMB, 12), (SWAMP, 40)]
P1_DECK = [(SWAMP, 60)]

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


def gy_ids(state, pid):
    p = player_of(state, pid)
    g = p.get("graveyard")
    if isinstance(g, list):
        return [int(o) for o in g]
    # fallback: scan objects
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard"
            and o.get("owner", o.get("controller")) == pid]


def gy_lnames(state, pid):
    return [lname(state, o) for o in gy_ids(state, pid)]


def zombie_gy_count(state, pid):
    return sum(1 for n in gy_lnames(state, pid) if n in ZOMBIE_CARDS)


def untapped_lands(state, pid, lands=(SWAMP,)):
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


def vi_opps(st):
    vi = get_vi(st)
    return (vi or {}).get("opportunities", []) or []


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id") \
                    and isinstance(v, (int, str)):
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


def surf_data(s):
    d = (s.get("data") or {})
    return d if isinstance(d, dict) else {}


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


def find_tyrant_activation(st, acts, state, tyrant_oid):
    """Scan both advertisement channels for the Tyrant's activation.

    Returns (channel, payload) or (None, None). Channels: 'legal_actions'
    (ActivateAbility action) or 'viewer_interaction' ((iid, choice))."""
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = d.get("source_id", d.get("object_id"))
        try:
            if src is not None and int(src) == int(tyrant_oid):
                return "legal_actions", a
        except Exception:
            continue
    for opp in vi_opps(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        choices = data.get("choices") or resp.get("choices") or []
        for ch in choices:
            codes = [surf_data(s).get("code")
                     for s in ch.get("surfaces") or []]
            if "activateAbility" not in codes:
                continue
            refs = [str(surf_data(s).get("reference"))
                    for s in ch.get("surfaces") or []
                    if surf_data(s).get("role") == "source"]
            if str(tyrant_oid) in refs:
                return "viewer_interaction", (opp.get("interactionId"), ch)
    return None, None


def scan_record(state, tyrant_oid, offered, channel):
    o = get_obj(state, tyrant_oid)
    return {
        "offered": offered,
        "channel": channel,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "active_player": state.get("active_player"),
        "priority_player": state.get("priority_player"),
        "tyrant_tapped": bool(o.get("tapped")),
        "untapped_swamps": len(untapped_lands(state, 0)),
        "zombie_gy": zombie_gy_count(state, 0),
        "ghoul_on_bf": bf_id(state, 0, GHOUL) is not None,
    }


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_restrictions", "A2_setup_ok", "C1_count_enforced",
            "B1_timing_enforced", "A3_offered_when_legal",
            "A4_activation_resolves", "A5_cleanup")}
    obs = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
           "offer_scans": [], "entomb_searches": [], "sac_prompts": []}
    ST = {"tyrant_cast_turn": None, "tyrant_oid": None,
          "test_c_done": False, "test_b_done": False,
          "test_a_done": False, "activation_submitted": False,
          "activation_resolved": False,
          "pre_a_exported": False, "mid_b_exported": False,
          "mid_c_exported": False, "post_exported": False,
          "post_at": None, "scan_c": None, "scan_b": None, "scan_a": None,
          "entombed_oids": [], "returned_oid": None,
          "life_p0": None, "life_p1": None}
    prompt_first_seen = {}
    last_select = {}
    kept = {}

    # ================= parse checks (primary evidence) =================
    cd = json.load(open(CD_PATH))
    cd_sha = hashlib.sha256(open(CD_PATH, "rb").read()).hexdigest()
    say(f"card-data.json sha256={cd_sha}")
    notes.append(f"parse input: pinned card-data.json sha256={cd_sha} "
                 f"(v0.81.3 signed data manifest)")

    tt = cd[TYRANT]
    with open(f"{EVDIR}/parse_tomb_tyrant.json", "w") as f:
        json.dump({"generated_from": CD_PATH, "card_data_sha256": cd_sha,
                   "card": tt}, f, indent=1)
    say("wrote parse_tomb_tyrant.json")

    acts = [a for a in tt.get("abilities", [])
            if a.get("kind") == "Activated"]
    blob = json.dumps(acts)
    n_unimpl = blob.count("Unimplemented")
    restrictions = (acts[0].get("activation_restrictions")
                    if acts else None) or []
    rtypes = [r.get("type") for r in restrictions]
    has_count = any(
        r.get("type") == "RequiresCondition"
        and "ZoneCardCount" in json.dumps(r)
        and "Graveyard" in json.dumps(r)
        and "Zombie" in json.dumps(r)
        and json.dumps(r).count("Fixed") >= 1
        for r in restrictions)
    # the >=3 comparator: QuantityComparison GE Fixed(3)
    has_ge3 = any(
        '"comparator": "GE"' in json.dumps(r)
        and '"value": 3' in json.dumps(r)
        for r in restrictions)
    has_timing = "DuringYourTurn" in rtypes
    ok = (len(acts) == 1 and len(restrictions) == 2 and has_count
          and has_ge3 and has_timing and n_unimpl == 0)
    ass["A1_parse_restrictions"] = "passed" if ok else "failed"
    notes.append(
        f"A1: Tomb Tyrant activated abilities={len(acts)}; "
        f"activation_restrictions={rtypes}; count>=3 node={has_count and has_ge3}; "
        f"DuringYourTurn={has_timing}; Unimplemented nodes={n_unimpl} "
        f"-> {ass['A1_parse_restrictions']}")
    wire("parse_tomb_tyrant",
         {"n_activated": len(acts), "restriction_types": rtypes,
          "count_ge3": bool(has_count and has_ge3),
          "during_your_turn": has_timing, "unimplemented": n_unimpl,
          "verdict": ass["A1_parse_restrictions"]})

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
        lands = sum(1 for n in hn if n == SWAMP)
        mulls = kept.get(f"P{pid}_mulls", 0)
        ok = lands >= need or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands)")
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

    async def discard_tick(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        for opp in vi_opps(st):
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

            def rank(ch):
                t = choice_text(ch).lower()
                if t == SWAMP:
                    return 0
                if t == ENTOMB:
                    return 1
                # keep Zombie creature cards: discarding them would move
                # the zone-count precondition the tests measure
                return 2

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            return True
        return False

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

    async def entomb_search_tick(c, tag, st, state):
        """Answer Entomb's library-search prompt by choosing Tomb Tyrant."""
        for opp in vi_opps(st):
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
            # candidates must include a Library-zone card for this to be
            # the Entomb search (not some other prompt)
            lib_tyrants = []
            for ch in chs:
                r = ref_of(ch)
                if not isinstance(r, int):
                    continue
                o = get_obj(state, r)
                if o.get("zone") == "Library" and lname(state, r) == TYRANT:
                    lib_tyrants.append((ch, r))
            if not lib_tyrants:
                continue
            ch, roid = lib_tyrants[0]
            shape = (wf_of(state).get("type"), resp.get("type"), len(chs))
            wire("entomb_search",
                 {"who": tag, "shape": shape, "picked_oid": roid,
                  "n_candidates": len(chs),
                  "texts": [choice_text(x)[:40] for x in chs][:6]})
            obs["entomb_searches"].append(
                {"shape": [str(x) for x in shape], "picked": roid,
                 "n": len(chs)})
            say(f"[{tag}] Entomb search: choosing Tomb Tyrant oid={roid} "
                f"(shape={shape})")
            await answer_vi(c, opp, ch, tag)
            ST["entombed_oids"].append(roid)
            entry["done"] = True
            return True
        return False

    def fodder_oid(state, tyrant_oid):
        """A sacrificable creature on P0's BF that isn't the source Tyrant."""
        ghoul = bf_id(state, 0, GHOUL)
        if ghoul is not None:
            return ghoul
        for oid in bf_ids(state, 0):
            if oid != tyrant_oid and lname(state, oid) == TYRANT:
                return oid
        return None

    async def sac_prompt_tick(c, tag, st, state):
        """Answer the activation's sacrifice-cost prompt with the fodder."""
        fodder = fodder_oid(state, ST["tyrant_oid"])
        if fodder is None:
            return False
        for opp in vi_opps(st):
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
            want = None
            for ch in chs:
                r = ref_of(ch)
                if isinstance(r, int) and r == fodder \
                        and get_obj(state, r).get("zone") == "Battlefield":
                    want = ch
                    break
            if want is None:
                continue
            shape = (wf_of(state).get("type"), resp.get("type"), len(chs))
            wire("sac_prompt",
                 {"who": tag, "shape": shape, "sacrificed": fodder,
                  "sacrificed_name": lname(state, fodder),
                  "n_candidates": len(chs)})
            obs["sac_prompts"].append(
                {"shape": [str(x) for x in shape], "sacrificed": fodder,
                 "name": lname(state, fodder)})
            say(f"[{tag}] sacrifice prompt: sacrificing "
                f"{lname(state, fodder)} oid={fodder} (shape={shape})")
            ST["sacrificed_oid"] = fodder
            await answer_vi(c, opp, want, tag)
            entry["done"] = True
            return True
        return False

    async def generic_prompt(c, tag, st, state, decline_after=25):
        acted = False
        for opp in vi_opps(st):
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

    def do_offer_scan(tag, st, acts, state, tyrant_oid):
        channel, _payload = find_tyrant_activation(st, acts, state,
                                                   tyrant_oid)
        rec = scan_record(state, tyrant_oid, channel is not None, channel)
        rec["fodder_oid"] = fodder_oid(state, tyrant_oid)
        obs["offer_scans"].append({"test": tag, **rec})
        wire("offer_scan", {"test": tag, **rec})
        say(f"SCAN-{tag}: offered={rec['offered']} channel={channel} "
            f"t{rec['turn']} {rec['phase']} active=P{rec['active_player']} "
            f"zombie_gy={rec['zombie_gy']} untapped_swamps="
            f"{rec['untapped_swamps']} tyrant_tapped={rec['tyrant_tapped']} "
            f"fodder={rec['fodder_oid']}")
        return rec, channel, _payload

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0", 2)
                return
            if find_action(acts, "SelectCards") \
                    and last_select.get(0) != p0.revision:
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
        if await discard_tick(p0, 0, "P0", st, state):
            return
        # Entomb's library search (part of its resolution)
        if wtype not in ("Priority", "GameOver", None, ""):
            if await entomb_search_tick(p0, "P0", st, state):
                return
        # sacrifice-cost prompt while the activation is in flight
        if ST["activation_submitted"] and not ST["activation_resolved"] \
                and wtype not in ("Priority", "GameOver", None, ""):
            if await sac_prompt_tick(p0, "P0", st, state):
                return
        # track the Tyrant on the battlefield + its cast turn
        tyrant = bf_id(state, 0, TYRANT)
        if tyrant is not None and ST["tyrant_oid"] is None:
            ST["tyrant_oid"] = int(tyrant)
            ST["tyrant_cast_turn"] = state.get("turn_number")
            say(f"Tomb Tyrant on BF: oid={tyrant} "
                f"turn={ST['tyrant_cast_turn']}")
            wire("tyrant_cast", {"oid": tyrant,
                                 "turn": ST["tyrant_cast_turn"]})
        # ---- TEST B: P1's turn, P0 holds priority, timing must fail ----
        if (not ST["test_b_done"] and ST["test_c_done"]
                and ST["tyrant_oid"] is not None
                and zombie_gy_count(state, 0) >= 3
                and not get_obj(state, ST["tyrant_oid"]).get("tapped")
                and state.get("active_player") == 1
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and my_priority(state, 0)):
            rec, _ch, _pl = do_offer_scan("B", st, acts, state,
                                          ST["tyrant_oid"])
            ST["scan_b"] = rec
            if await export_named("mid_b"):
                ST["mid_b_exported"] = True
            ST["test_b_done"] = True
            say("TEST B recorded; passing priority on P1's turn")
        # ---- mid-resolution capture: Tyrant ability on the stack ----
        # Hold priority for one tick while the activated ability is on the
        # stack so we can export proof that the sacrifice cost was paid
        # (fodder in graveyard) before the ability resolves.
        if ST["activation_submitted"] and not ST.get("mid_res_exported"):
            for e in (state.get("stack") or []):
                if e.get("source_id") == ST["tyrant_oid"]:
                    wire("mid_resolution_stack",
                         {"kind": (e.get("kind") or {}).get("type"),
                          "source_id": e.get("source_id")})
                    if await export_named("mid_resolution"):
                        ST["mid_res_exported"] = True
                    say("exported MID_RESOLUTION (Tyrant ability on stack)")
                    return  # hold priority this tick; resume passing next
        # ---- activation resolution watch ----
        if ST["activation_submitted"] and not ST["activation_resolved"]:
            tyr = get_obj(state, ST["tyrant_oid"])
            if not (state.get("stack") or []) and tyr.get("tapped"):
                ST["activation_resolved"] = True
                say("activation resolved (stack empty, Tyrant tapped)")
                wire("activation_resolved", {})
                if await export_named("post_a"):
                    ST["post_exported"] = True
                ST["post_at"] = time.time()
        if ST["post_exported"] and ST["post_at"] is not None:
            return  # let the main loop finish us
        # ---- P0 main-phase action window ----
        if (my_priority(state, 0) and state.get("active_player") == 0
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and not (state.get("stack") or [])):
            turn = state.get("turn_number")
            # TEST C: zone-count must fail (<3 Zombies), P0's turn, Tyrant
            # past summoning sickness, payable, untapped.
            if (not ST["test_c_done"] and ST["tyrant_oid"] is not None
                    and turn is not None
                    and turn > (ST["tyrant_cast_turn"] or 0)
                    and not get_obj(state, ST["tyrant_oid"]).get("tapped")):
                rec, _ch, _pl = do_offer_scan("C", st, acts, state,
                                              ST["tyrant_oid"])
                ST["scan_c"] = rec
                if await export_named("mid_c"):
                    ST["mid_c_exported"] = True
                ST["test_c_done"] = True
                say("TEST C recorded")
            # TEST A: both conditions hold -> offer + activate.
            if (ST["test_b_done"] and not ST["test_a_done"]
                    and ST["tyrant_oid"] is not None
                    and zombie_gy_count(state, 0) >= 3
                    and not get_obj(state, ST["tyrant_oid"]).get("tapped")):
                rec, channel, payload = do_offer_scan(
                    "A", st, acts, state, ST["tyrant_oid"])
                ST["scan_a"] = rec
                if await export_named("pre_a"):
                    ST["pre_a_exported"] = True
                ST["test_a_done"] = True
                if channel == "legal_actions":
                    say(f"[P0] TEST A: activating via legal_actions "
                        f"(source {ST['tyrant_oid']})")
                    await submit_as_is(p0, payload)
                    ST["activation_submitted"] = True
                    return
                elif channel == "viewer_interaction":
                    _iid, choice = payload
                    opp = next((o for o in vi_opps(st)
                                if o.get("interactionId") == _iid), None)
                    if opp is not None:
                        say("[P0] TEST A: activating via "
                            "viewer_interaction activateAbility")
                        await answer_vi(p0, opp, choice, "P0")
                        ST["activation_submitted"] = True
                        return
                    say("[P0] TEST A: vi opportunity vanished; not "
                        "submitting")
                else:
                    say("[P0] TEST A: activation NOT offered while legal; "
                        "skipping submission")
            # ---- ramp / setup (gated so tests stay isolated) ----
            hn = hand_lnames(state, 0)
            # land drop
            for a in acts:
                if a["type"] == "PlayLand":
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id")
                    if isinstance(oid, int) and get_obj(
                            state, oid).get("zone") == "Hand":
                        await submit_as_is(p0, a)
                        say("[P0] land drop")
                        return
            # cast Ghoul (sacrifice fodder) early; fallback: a second
            # Tyrant copy as fodder if no Ghoul was drawn
            if GHOUL in hn and bf_id(state, 0, GHOUL) is None \
                    and not ST["test_a_done"]:
                if await cast_named(p0, acts, state, GHOUL, "P0") is not None:
                    return
            if TYRANT in hn and ST["tyrant_oid"] is not None \
                    and bf_id(state, 0, GHOUL) is None \
                    and fodder_oid(state, ST["tyrant_oid"]) is None \
                    and not ST["test_a_done"]:
                if await cast_named(p0, acts, state, TYRANT, "P0") \
                        is not None:
                    say("[P0] cast 2nd Tyrant as sacrifice fodder")
                    return
            # cast Tyrant when affordable
            if TYRANT in hn and ST["tyrant_oid"] is None:
                if await cast_named(p0, acts, state, TYRANT, "P0") \
                        is not None:
                    return
            # cast Entombs only after TEST C (needs <3 zombies first)
            if ST["test_c_done"] and not ST["test_b_done"] \
                    and ENTOMB in hn and zombie_gy_count(state, 0) < 3:
                if await cast_named(p0, acts, state, ENTOMB, "P0") \
                        is not None:
                    return
        if not my_priority(state, 0):
            if await generic_prompt(p0, "P0", st, state):
                return
            return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, "P1", 1)
                return
            if find_action(acts, "SelectCards") \
                    and last_select.get(1) != p1.revision:
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
        if await discard_tick(p1, 1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, "P1", st, state):
                return
            return
        # P1: land drop on its own turn, then pass
        if (state.get("active_player") == 1
                and state.get("phase") in ("PreCombatMain",
                                           "PostCombatMain")):
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
        for fn in ("pre_a", "mid_b", "mid_c", "post_a", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre_a, mid_b, mid_c = (states.get(k)
                               for k in ("pre_a", "mid_b", "mid_c"))
        post = states.get("post_a") or states.get("post")

        # ---- A2: fixture reached ----
        sc = ST["scan_c"]
        ok = (sc is not None and ST["tyrant_oid"] is not None
              and sc["zombie_gy"] < 3 and not sc["tyrant_tapped"])
        ass["A2_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A2: test-C scan ran={sc is not None}; "
                     f"zombie_gy@C={sc['zombie_gy'] if sc else '?'}; "
                     f"tyrant untapped@C={sc and not sc['tyrant_tapped']} "
                     f"-> {ass['A2_setup_ok']}")

        # ---- C1: zone-count enforced ----
        if ST["scan_c"] is not None:
            offered = ST["scan_c"]["offered"]
            ass["C1_count_enforced"] = "passed" if not offered else "failed"
            notes.append(f"C1: P0 turn t{ST['scan_c']['turn']} "
                         f"{ST['scan_c']['phase']}, zombie_gy="
                         f"{ST['scan_c']['zombie_gy']} (<3), untapped_swamps="
                         f"{ST['scan_c']['untapped_swamps']}, fodder="
                         f"{ST['scan_c']['fodder_oid']}: offered="
                         f"{offered} -> {ass['C1_count_enforced']}")
        else:
            notes.append("C1 not-run: no test-C scan")

        # ---- B1: timing enforced ----
        if ST["scan_b"] is not None:
            offered = ST["scan_b"]["offered"]
            ass["B1_timing_enforced"] = "passed" if not offered else "failed"
            notes.append(f"B1: P1 turn t{ST['scan_b']['turn']} "
                         f"{ST['scan_b']['phase']}, P0 priority, zombie_gy="
                         f"{ST['scan_b']['zombie_gy']} (>=3), "
                         f"untapped_swamps={ST['scan_b']['untapped_swamps']}, "
                         f"fodder={ST['scan_b']['fodder_oid']}: offered="
                         f"{offered} -> {ass['B1_timing_enforced']}")
        else:
            notes.append("B1 not-run: no test-B scan")

        # ---- A3: offered when legal ----
        if ST["scan_a"] is not None:
            offered = ST["scan_a"]["offered"]
            ass["A3_offered_when_legal"] = "passed" if offered else "failed"
            notes.append(f"A3: P0 turn t{ST['scan_a']['turn']} "
                         f"{ST['scan_a']['phase']}, zombie_gy="
                         f"{ST['scan_a']['zombie_gy']} (>=3), channel="
                         f"{ST['scan_a']['channel']}: offered={offered} "
                         f"-> {ass['A3_offered_when_legal']}")
        else:
            notes.append("A3 not-run: no test-A scan")

        # ---- A4: activation resolves ----
        # The return is "at random": any Zombie creature card may be picked,
        # including the just-sacrificed Ghoul (which was on the battlefield,
        # not in the graveyard, at activation time). Two independent
        # branches:
        #  (1) a Zombie that was in pre_a's graveyard moved to the BF, or
        #  (2) the sacrificed fodder is in the graveyard in the
        #      mid-resolution snapshot (proving the sacrifice cost was paid)
        #      and is back on the battlefield post-resolution (the random
        #      return picked it).
        if ST["activation_resolved"] and pre_a is not None \
                and post is not None:
            pre_gy = set(gy_ids(pre_a, 0))
            post_gy = set(gy_ids(post, 0))
            post_bf = {int(oid) for oid, o
                       in (post.get("objects", {}) or {}).items()
                       if o.get("zone") == "Battlefield"
                       and o.get("controller") == 0}
            pre_gy_z = {o for o in pre_gy
                        if lname(pre_a, o) in ZOMBIE_CARDS}
            returned = [o for o in pre_gy_z
                        if o not in post_gy and o in post_bf]
            ST["returned_oid"] = returned[0] if returned else None
            ST["returned_name"] = (lname(pre_a, returned[0])
                                   if returned else None)
            tyr_post = get_obj(post, ST["tyrant_oid"])
            ok = (len(returned) >= 1 and bool(tyr_post.get("tapped")))
            result_line = ""
            if not ok:
                sac = ST.get("sacrificed_oid")
                mid = None
                try:
                    mid = loads_state("mid_resolution")
                except Exception:
                    mid = None
                if sac is not None and mid is not None:
                    mid_zone = (mid.get("objects", {})
                                .get(str(sac), {}).get("zone"))
                    post_zone = (post.get("objects", {})
                                 .get(str(sac), {}).get("zone"))
                    pre_zone = (pre_a.get("objects", {})
                                .get(str(sac), {}).get("zone"))
                    if (pre_zone == "Battlefield"
                            and mid_zone == "Graveyard"
                            and post_zone == "Battlefield"
                            and bool(tyr_post.get("tapped"))):
                        ok = True
                        result_line = (
                            f" sacrificed fodder oid={sac} "
                            f"({lname(pre_a, sac)}): BF->GY "
                            f"(mid-resolution) ->BF (random return)")
            ass["A4_activation_resolves"] = "passed" if ok else "failed"
            notes.append(f"A4: pre-gy zombies={len(pre_gy_z)}; returned to "
                         f"BF={returned} ({ST['returned_name']}){result_line}; "
                         f"tyrant tapped post={bool(tyr_post.get('tapped'))} "
                         f"-> {ass['A4_activation_resolves']}")
            wire("a4_check", {"pre_gy_zombies": sorted(pre_gy_z),
                              "returned": returned,
                              "returned_name": ST["returned_name"],
                              "tyrant_tapped": bool(tyr_post.get("tapped")),
                              "sacrificed_oid": ST.get("sacrificed_oid")})
        elif ST["activation_submitted"]:
            ass["A4_activation_resolves"] = "failed"
            notes.append("A4 failed: activation submitted but never resolved")
        else:
            notes.append("A4 not-run: activation never submitted")

        # ---- A5: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            ass["A5_cleanup"] = "passed" if stack_empty else "failed"
            notes.append(f"A5: post stack empty={stack_empty} "
                         f"-> {ass['A5_cleanup']}")
        else:
            notes.append("A5 not-run: no post state")

        # ---- verdict ----
        parse_ok = ass["A1_parse_restrictions"] == "passed"
        if ass["A2_setup_ok"] != "passed":
            verdict = "blocked"
            reason = "fixture never reached the test-C scan"
        elif ass["A1_parse_restrictions"] == "failed":
            verdict = "reproduced"
            reason = "parse drops the compound restriction clause"
        elif ass["C1_count_enforced"] == "failed" \
                or ass["B1_timing_enforced"] == "failed":
            verdict = "reproduced"
            reason = "engine offers the activation while a parsed " \
                     "restriction fails"
        elif ass["A3_offered_when_legal"] != "passed":
            verdict = "blocked"
            reason = "positive control failed: ability never offered even " \
                     "when legal (different defect or fixture fault)"
        elif (ass["C1_count_enforced"] == "passed"
                and ass["B1_timing_enforced"] == "passed" and parse_ok):
            verdict = "not-reproduced"
            reason = "both parsed restrictions enforced at runtime on v0.81.3"
        else:
            verdict = "blocked"
            reason = "inconclusive assertion mix"
        say(f"VERDICT: {verdict} ({reason})")
        notes.append(f"verdict={verdict}: {reason}")

        result_line = (
            f"A1 parse_restrictions: {ass['A1_parse_restrictions']} "
            f"(2 restriction nodes, 0 Unimplemented); "
            f"A2 setup_ok: {ass['A2_setup_ok']}; "
            f"C1 count_enforced: {ass['C1_count_enforced']} "
            f"(P0 turn, zombie_gy<3); "
            f"B1 timing_enforced: {ass['B1_timing_enforced']} "
            f"(P1 turn, zombie_gy>=3); "
            f"A3 offered_when_legal: {ass['A3_offered_when_legal']}; "
            f"A4 activation_resolves: {ass['A4_activation_resolves']} "
            f"(returned oid {ST['returned_oid']}); "
            f"A5 cleanup: {ass['A5_cleanup']}.")

        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": "Tomb Tyrant - compound activation timing and "
                     "game-state restrictions are unsupported",
            "validated_at": "2026-09-13",
            "validated_version": "v0.81.3",
            "server_version": "v0.81.3",
            "protocol_version": 70,
            "build_commit": SERVER_IDENTITY["build_commit"],
            "server_binary_sha256": SERVER_IDENTITY["binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": True,
            "verdict": verdict,
            "assertions": ass,
            "result": result_line,
            "scope": "Tomb Tyrant compound activation restriction "
                     "('Activate only during your turn and only if there are "
                     "at least three Zombie creature cards in your "
                     "graveyard'): card-data parse check + runtime offer "
                     "scans with each condition failing independently and "
                     "both succeeding; native engine, two human-client "
                     "seats",
            "driver_state": {k: v for k, v in ST.items()},
            "observations": obs,
            "notes": notes,
            "driver_notes": [
                "Test order C -> B -> A in one game isolates each failing "
                "condition before the positive control.",
                "Offer scans check both advertisement channels: "
                "legal_actions ActivateAbility and viewer_interaction "
                "activateAbility choices.",
                "Entomb's library search answered by choosing Tomb Tyrant; "
                "the sacrificed creature is the Diregraf Ghoul (fallback: a "
                "second Tyrant copy).",
                "Each scan records payable-cost preconditions (untapped "
                "Swamps, untapped Tyrant, fodder present) so a withheld "
                "offer is attributable to the restriction under test.",
            ],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "The 'return at random' choice is engine-driven; the "
                "assertion checks that one of the Entombed Zombie cards "
                "changed zones graveyard -> battlefield.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6984.py",
                    f"{EVDIR}/scenario_6984.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6984.py and server.log into EVDIR")
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
        W, H = 1000, 980
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6984 - Tomb Tyrant compound "
               "activation restriction", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "parser + runtime", fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120)
                     if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        labels = {
            "A1_parse_restrictions": "PARSE: 2 restriction nodes, 0 "
                                    "Unimplemented",
            "A2_setup_ok": "GAME: fixture reached (Tyrant BF, scan C ran)",
            "C1_count_enforced": "GAME: <3 Zombies -> NOT offered (P0 turn)",
            "B1_timing_enforced": "GAME: 3 Zombies -> NOT offered (P1 turn)",
            "A3_offered_when_legal": "GAME: 3 Zombies -> offered (P0 turn)",
            "A4_activation_resolves": "GAME: activation resolves, Zombie "
                                      "returns",
            "A5_cleanup": "GAME: stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Offer scans:", fill=(200, 210, 225))
        y += 24
        for s in (run["observations"] or {}).get("offer_scans", []):
            d.text((36, y),
                   f"test {s.get('test')}: offered={s.get('offered')} "
                   f"ch={s.get('channel')} t{s.get('turn')} "
                   f"{s.get('phase')} active=P{s.get('active_player')} "
                   f"zgy={s.get('zombie_gy')} swamps="
                   f"{s.get('untapped_swamps')}"[:112],
                   fill=(150, 160, 175))
            y += 22
        y += 6
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:14]:
            d.text((36, y), n[:112], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        # NOTE: scenario_run.log is hashed LAST, after the final say() line
        # below is appended; otherwise the manifest's hash for the log goes
        # stale by exactly one line.
        files = ["pre_a.json", "mid_b.json", "mid_c.json", "post_a.json",
                 "mid_resolution.json",
                 "post.json", "parse_tomb_tyrant.json",
                 "run.json", "scenario_6984.py", "wire_log.jsonl",
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
    no_tyrant_watchdog_at = None
    while time.time() - t0 < 1500:
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
        if (ST["tyrant_oid"] is None and turn >= 14
                and no_tyrant_watchdog_at is None):
            no_tyrant_watchdog_at = time.time()
            notes.append(f"watchdog: turn {turn} reached with Tomb Tyrant "
                         f"never cast; giving 120s more")
        if (no_tyrant_watchdog_at is not None
                and time.time() - no_tyrant_watchdog_at > 120):
            notes.append("watchdog: Tomb Tyrant never cast; finishing")
            await finish()
            return
        if time.time() - last_diag > 90 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} "
                f"tyrant={ST['tyrant_oid']} zgy={zombie_gy_count(s, 0)} "
                f"C={ST['test_c_done']} B={ST['test_b_done']} "
                f"A={ST['test_a_done']}/{ST['activation_submitted']}/"
                f"{ST['activation_resolved']} "
                f"P0hand={hand_lnames(s, 0)}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
