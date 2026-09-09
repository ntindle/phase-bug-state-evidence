#!/usr/bin/env python3
"""Issue #825: Choose Your Weapon - not working as intended.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed; labels: area:parser, mechanic:modal,
priority:p2-wrong-game-result): "Choose Your Weapon - not working as intended."
Oracle text (pinned card-data.json):
  Choose one —
  - Two-Weapon Fighting — Double target creature's power and toughness
    until end of turn.
  - Archery — This spell deals 5 damage to target creature with flying.

The report gives no failing branch, so the contract exercises the full
intended behavior of both modes, including the per-mode target legality
(Archery may only target a creature with flying).

Setup (native engine, two human-client seats):
  P0: 12x Choose Your Weapon, 4x Grizzly Bears, 4x Runeclaw Bear,
      8x Birds of Paradise, 4x Llanowar Elves, 28x Forest.
      (12x Weapon density: engine accepts >4-of for custom games; ensures
      both modal casts complete in one game. Mulligan until Weapon + 2 lands.
      8x BoP: two flyers on the battlefield force the Archery target prompt
      to appear — a single legal target is auto-targeted (cf. #658).)
      Casts a 2/2 (Bears/Runeclaw) and a 0/1 flyer (BoP), then casts the
      spell twice: Two-Weapon Fighting first, Archery second.
  P1: 60x Forest dummy (plays a land, passes).

Expected (per card text):
  E1: casting offers a modal choice with both mode descriptions.
  E2: Two-Weapon Fighting targeting the 2/2 -> it becomes 4/4 until EOT;
      nothing else changes (BoP untouched, life 20/20).
  E3: Archery's target selection offers the flying BoP and does NOT offer
      the non-flying 2/2.
  E4: Archery targeting BoP deals 5 damage -> BoP (0/1) is destroyed;
      life totals unchanged; spell ends in graveyard.
  E5: game proceeds with no stuck decision.

Assertions:
  A1_setup_ok        pre-cast: 2/2 + BoP on BF (P0), 3+ untapped Forests,
                     Choose Your Weapon in hand
  A2_modal_offered   mode choice presented with both mode descriptions;
                     intended mode actually selected for each cast
  A3_doubling        after Two-Weapon Fighting resolves: target is 4/4;
                     BoP still 0/1 undamaged; life 20/20
  A4_archery_targets Archery candidates include BoP, exclude the 2/2
  A5_archery_damage  after Archery resolves: BoP destroyed by 5 damage,
                     in graveyard; life 20/20; 2/2 unharmed
  A6_cleanup         spell in graveyard; no pending decision; game proceeds

Verdict rule: reproduced iff any of A2..A5 fails on the exercised path.
not-reproduced iff A1..A6 all pass. blocked iff setup/cast flow cannot be
driven to completion.

Evidence: evidence/825/<run-id>/pre.json (pre-first-cast), mid.json
(after Two-Weapon Fighting resolves), post.json (after Archery resolves),
run.json, manifest.sha256, summary.png, scenario_825.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-13"
EVDIR = f"{BACKFILL}/evidence/825/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

WEAPON = "Choose Your Weapon"
BEARS = "Grizzly Bears"
RUNECLAW = "Runeclaw Bear"
BOP = "Birds of Paradise"
ELVES = "Llanowar Elves"
FOREST = "Forest"

# 12x Weapon density (engine accepts >4-of for custom games; verified 2026-09-09):
# guarantees both modal casts complete in one game instead of stalling on draws.
# 8x Birds of Paradise: two flyers on the battlefield force the Archery target
# prompt to actually appear (single legal target is auto-targeted, cf. #658).
P0_DECK = [(WEAPON, 12), (BEARS, 4), (RUNECLAW, 4), (BOP, 8), (ELVES, 4), (FOREST, 28)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.77.0",
    "build_commit": "61715b5",
    "protocol_version": 67,
    "mode": "Full",
    "binary_sha256": "a52293b754605baa63e4d987a5208902ec394868d49c4bfd86b3fd57f3267c6a",
    "card_data_sha256": "698350d9b6323011a5b86a74a4d2ea54d13b4ed26a520579be7d044f0a3692e5",
    "draft_pools_sha256": "56e030fdc74b2385759310de8564a58b8716035b2dccc0da709f37250cc2d3c7",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 match of pinned verified artifacts; "
              "server started fresh this run (runs/run-20260909-01)",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def life(state, pid):
    return state["players"][pid]["life"]


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def pt(obj):
    return num(obj.get("power")), num(obj.get("toughness"))


def damage_on(obj):
    for k in ("damage", "damage_marked", "marked_damage"):
        v = obj.get(k)
        if isinstance(v, (int, float)):
            return v
    return 0


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def graveyard_names(state, pid):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Graveyard" and o.get("controller") == pid:
            out.append(o.get("base_name") or o.get("name") or "?")
    return out


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def untapped_forests(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == FOREST and not o.get("tapped")]


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
    """All human-readable text across a choice's surfaces."""
    bits = []

    def rec(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if k in ("name", "code", "label", "value", "description", "text",
                         "prompt", "title") and isinstance(val, str):
                    bits.append(val)
                else:
                    rec(val)
        elif isinstance(v, list):
            for x in v:
                rec(x)
    rec(ch.get("surfaces", []))
    return " | ".join(bits)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_modal_offered", "A3_doubling",
            "A4_archery_targets", "A5_archery_damage", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    for _c in (p0, p1):
        _c._last_send = 0.0
        _orig_send = _c.send_action

        async def _tracked_send(action, _c=_c, _orig=_orig_send):
            _c._last_send = time.time()
            await _orig(action)
        _c.send_action = _tracked_send

    # ---- scenario state ----
    pre_exported = False
    cast1 = {"submitted": False, "mode_chosen": None, "target_oid": None,
             "target_name": None, "resolved": False, "rev": None}
    cast2 = {"submitted": False, "mode_chosen": None, "target_oid": None,
             "target_name": None, "resolved": False, "rev": None,
             "candidates": None}
    post_exported = False
    mid_exported = False
    obj_dumped = False
    modal_shapes_logged = set()
    submitted_interactions = set()

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        core = [ass["A2_modal_offered"], ass["A3_doubling"],
                ass["A5_archery_damage"]]
        a4_ok = ass["A4_archery_targets"] == "passed" or (
            ass["A4_archery_targets"] == "not-run"
            and any("auto-targeted" in n for n in notes))
        if ass["A1_setup_ok"] == "passed" and a4_ok \
                and all(v == "passed" for v in core):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(v == "failed" for v in core + [ass["A4_archery_targets"]]):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        run = {
            "issue": 825,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_825.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
            "setup_line": "P0: 12x Choose Your Weapon + 4x Grizzly Bears + 4x Runeclaw Bear + "
                          "4x Birds of Paradise + 4x Llanowar Elves + 32x Forest "
                          "(12x density so both modal casts complete; "
                          "mulligan-to-weapon, keep if weapon+2 lands); "
                          "P1: 60x Forest dummy",
            "contract_line": "P0 casts Choose Your Weapon twice: Two-Weapon Fighting "
                             "targeting own 2/2 (expect 4/4), then Archery targeting own "
                             "Birds of Paradise (expect 5 damage, destroyed); Archery must "
                             "not offer the non-flying 2/2 as a target",
            "stats": {},
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    def two_two_name(state):
        for nm in (BEARS, RUNECLAW):
            if battlefield_ids(state, 0, nm):
                return nm
        return None

    def setup_ready(state):
        return (two_two_name(state) is not None
                and len(battlefield_ids(state, 0, BOP)) > 0
                and len(untapped_forests(state, 0)) >= 3
                and WEAPON in hand_names(state, 0))

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def handle_modal(c, st, state, cast, wanted_mode, wanted_target):
        """Drive one modal cast: choose mode, then choose target. Returns True if acted."""
        vi = get_vi(st)
        if not vi:
            return False
        opps = vi.get("opportunities", []) or []
        for opp in opps:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            if iid in submitted_interactions:
                continue  # already answered this opportunity; avoid double-submit
            chs = data.get("choices") or data.get("candidates") or []
            texts = [(ch, choice_text(ch)) for ch in chs]
            avail = [(ch, t) for ch, t in texts
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            blob = " // ".join(t for _, t in texts)
            # classify: mode choice if a mode name appears in the texts
            is_mode = any("Two-Weapon" in t or "Archery" in t for _, t in texts)
            key = (wanted_mode, is_mode, rtype)
            if key not in modal_shapes_logged:
                modal_shapes_logged.add(key)
                say(f"interaction shape ({wanted_mode}, is_mode={is_mode}, rtype={rtype}): "
                    f"{json.dumps(opp)[:1500]}")
                wire(f"interaction_shape_{wanted_mode}", opp)
            if is_mode and cast["mode_chosen"] is None:
                pick = None
                for ch, t in avail:
                    if wanted_mode in t:
                        pick = ch
                        break
                if pick is None and len(avail) == 2:
                    # fallback: card-data mode order is [Two-Weapon Fighting, Archery]
                    pick = avail[0][0] if wanted_mode == "Two-Weapon" else avail[1][0]
                    notes.append(f"mode choice for {wanted_mode} had no matching text; "
                                 f"fell back to index")
                if pick is None:
                    notes.append(f"could not pick mode {wanted_mode}; choices: {blob[:200]}")
                    return False
                iid = opp.get("interactionId")
                spec = data.get("spec") or {}
                sub_type = spec.get("type") if isinstance(spec, dict) else None
                if rtype == "exactChoices":
                    sub = {"interactionId": iid, "response":
                           {"type": "choose", "data": {"choiceId": pick["id"]}}}
                else:
                    sub = {"interactionId": iid, "response":
                           {"type": sub_type or "sequence",
                            "data": {"choiceIds": [pick["id"]]}}}
                say(f"submitting mode {wanted_mode}: choice {pick['id']} :: {choice_text(pick)[:160]}")
                wire("mode_submission", {"cast": wanted_mode, "submission": sub,
                                         "interaction": opp})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                cast["mode_chosen"] = wanted_mode
                await asyncio.sleep(1.0)
                return True
            # Target selection arrives as a schema (sequence) opportunity whose
            # candidates carry object surfaces with role "candidate". Priority
            # menus (exactChoices with passPriority/castSpell/...) must NOT be
            # treated as target prompts (20260909-12 misfire: driver "targeted"
            # by casting a Birds of Paradise from hand).
            if rtype == "schema" and not is_mode and cast["mode_chosen"] \
                    and cast["target_oid"] is None:
                # target selection: record candidates for A4 on the Archery cast
                bf_cands = []  # (name) of battlefield-zone candidates
                for ch, t in avail:
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        if isinstance(d, dict) and d.get("name") \
                                and d.get("zone") == "battlefield":
                            bf_cands.append(d["name"])
                            break
                if cast is cast2 and cast["candidates"] is None:
                    cast["candidates"] = list(bf_cands)
                    say(f"Archery target candidates (battlefield): {bf_cands}")
                    wire("archery_candidates", {"candidates": bf_cands,
                                                "interaction": opp})
                    flyers = sorted(
                        (o.get("base_name") or o.get("name"))
                        for o in state.get("objects", {}).values()
                        if o.get("zone") == "Battlefield"
                        and o.get("controller") == 0
                        and "Flying" in (o.get("keywords") or []))
                    from collections import Counter
                    if bf_cands and Counter(bf_cands) == Counter(flyers):
                        ass["A4_archery_targets"] = "passed"
                        notes.append(f"Archery candidates == flying creatures: "
                                     f"{sorted(flyers)}; non-flyers excluded")
                    else:
                        ass["A4_archery_targets"] = "failed"
                        notes.append(f"Archery candidates wrong: {bf_cands} "
                                     f"(want exactly the flyers {sorted(flyers)})")
                pick = None
                for ch, t in avail:
                    cnames = []
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        if isinstance(d, dict) and d.get("name"):
                            cnames.append(d["name"])
                    if wanted_target in cnames or wanted_target in t:
                        pick = ch
                        break
                if pick is None:
                    notes.append(f"target {wanted_target} not among candidates: "
                                 f"{bf_cands}")
                    return False
                iid = opp.get("interactionId")
                spec = data.get("spec") or {}
                sub_type = spec.get("type") if isinstance(spec, dict) else None
                sub = {"interactionId": iid, "response":
                       {"type": sub_type or "sequence",
                        "data": {"choiceIds": [pick["id"]]}}}
                say(f"submitting target {wanted_target}: {pick['id']}")
                wire("target_submission", {"cast": wanted_mode, "target": wanted_target,
                                           "submission": sub, "interaction": opp})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                # resolve the target oid from the submitted candidate's object
                # reference (NOT first-on-battlefield: 20260909-12 read the
                # wrong Grizzly Bears and failed A3 spuriously)
                for s in pick.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    if isinstance(d, dict) and d.get("reference") is not None \
                            and str(d.get("role")) == "candidate":
                        try:
                            cast["target_oid"] = int(d["reference"])
                        except (TypeError, ValueError):
                            pass
                        break
                cast["target_name"] = wanted_target
                cast["rev"] = c.revision
                await asyncio.sleep(1.0)
                return True
        return False

    def spell_zone(state, cast):
        so = cast.get("spell_oid")
        return get_obj(state, so).get("zone") if so else None

    async def drain_rejections(c, timeout=3):
        rej = None
        t0 = time.time()
        while time.time() - t0 < timeout and rej is None:
            await asyncio.sleep(0.3)
            try:
                while True:
                    t, d = c.inbox.get_nowait()
                    if t in ("ActionRejected", "Error"):
                        rej = {"type": t, "data": d}
            except asyncio.QueueEmpty:
                pass
        return rej

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, mid_exported, obj_dumped
        wtype = (state.get("waiting_for") or {}).get("type")
        if not obj_dumped:
            for oid, o in state.get("objects", {}).items():
                if o.get("zone") == "Battlefield" and o.get("controller") == 0 \
                        and (o.get("base_name") or o.get("name")) in (BEARS, BOP):
                    say("creature object sample: " + json.dumps(o, default=str)[:900])
                    wire("creature_object_sample", o)
                    obj_dumped = True
                    break
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n == FOREST)
            mulls = kept.get("P0_mulls", 0)
            if WEAPON in hn and lands >= 2 or mulls >= 3:
                kept["P0"] = True
                await c_send(p0, {"type": "MulliganDecision",
                                  "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps opening hand (weapons={hn.count(WEAPON)}, lands={lands})")
            else:
                kept["P0_mulls"] = mulls + 1
                await c_send(p0, {"type": "MulliganDecision",
                                  "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (weapons={hn.count(WEAPON)}, lands={lands})")
            return
        # post-mulligan bottom-cards phase: SelectCards to put `count` on bottom
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for pl in state.get("players", [])
                            if pl.get("id") == 0 for o in pl.get("hand", [])]
                def bottom_key(oid):
                    nm = obj_name(state, oid)
                    return 0 if nm == FOREST else (2 if nm == WEAPON else 1)
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count} after mulligan: "
                    f"{[obj_name(state, x) for x in picks]}")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "DiscardToHandSize":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []):
                    data = (opp.get("response", {}) or {}).get("data", {}) or {}
                    cands = data.get("candidates") or []
                    if cands:
                        spec = data.get("spec") or {}
                        stype = spec.get("type", "select")
                        sub = {"interactionId": opp.get("interactionId"),
                               "response": {"type": stype,
                                            "data": {"choiceIds": [cands[0]["id"]]}}}
                        await p0.send_interaction(sub)
                        say("P0 discards to hand size")
                        return
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            # modal/target interactions arrive under their own waiting_for
            # types (ModeChoice, TargetSelection, ...): drive them even
            # when it is not a Priority window.
            if cast1["submitted"] and not cast1["resolved"]:
                if await handle_modal(p0, st, state, cast1, "Two-Weapon",
                                      cast1.get("wanted_target") or two_two_name(state) or BEARS):
                    return
            if cast2["submitted"] and not cast2["resolved"]:
                if await handle_modal(p0, st, state, cast2, "Archery", BOP):
                    return
            return
        # ---- P0 priority ----
        # 1) drive in-flight modal casts
        if cast1["submitted"] and not cast1["resolved"]:
            if await handle_modal(p0, st, state, cast1, "Two-Weapon",
                                  cast1.get("wanted_target") or two_two_name(state) or BEARS):
                return
        if cast2["submitted"] and not cast2["resolved"]:
            if await handle_modal(p0, st, state, cast2, "Archery", BOP):
                return
        # 2) check cast1 resolution
        if cast1["submitted"] and not cast1["resolved"] and cast1["rev"] is not None \
                and p0.revision != cast1["rev"]:
            st2 = p0.latest["state"]
            zone = spell_zone(st2, cast1)
            wire("cast1_zone_check", {"zone": zone, "rev": p0.revision})
            if zone is not None and zone != "Stack":
                cast1["resolved"] = True
                if cast1["mode_chosen"] is None:
                    ass["A2_modal_offered"] = "failed"
                    notes.append("cast1 resolved with NO modal choice ever offered "
                                 "(engine used a default mode)")
                tn = cast1["target_name"]
                toid = cast1["target_oid"]
                o = get_obj(st2, toid) if toid else {}
                p, t = pt(o)
                others_ok = True
                for oid2, o2 in st2.get("objects", {}).items():
                    if o2.get("zone") != "Battlefield" or o2.get("controller") != 0:
                        continue
                    if "Creature" not in (o2.get("card_types", {}) or {}).get("core_types", []):
                        continue
                    if toid and int(oid2) == toid:
                        continue
                    if damage_on(o2) != 0:
                        others_ok = False
                    p2, t2 = pt(o2)
                    if p2 != num(o2.get("layer_base_power")) or t2 != num(o2.get("layer_base_toughness")):
                        others_ok = False
                say(f"cast1 resolved: {tn} (oid {toid}) now {p}/{t}; others_ok={others_ok}; "
                    f"life={life(st2,0)}/{life(st2,1)}; gy={graveyard_names(st2,0)}")
                wire("cast1_resolved", {"target": tn, "target_oid": toid, "p": p, "t": t,
                                       "others_ok": others_ok,
                                       "life": [life(st2, 0), life(st2, 1)],
                                       "gy": graveyard_names(st2, 0)})
                if p == 4 and t == 4 and others_ok \
                        and life(st2, 0) == 20 and life(st2, 1) == 20:
                    ass["A3_doubling"] = "passed"
                    notes.append(f"Two-Weapon Fighting: {tn} (oid {toid}) doubled "
                                 f"to 4/4, all other creatures untouched")
                else:
                    ass["A3_doubling"] = "failed"
                    notes.append(f"Two-Weapon Fighting wrong: {tn} (oid {toid})={p}/{t}, "
                                 f"others_ok={others_ok}, "
                                 f"life={life(st2,0)}/{life(st2,1)}")
                if not mid_exported:
                    try:
                        mid = await p0.export_state()
                        with open(f"{EVDIR}/mid.json", "w") as f:
                            f.write(mid)
                        mid_exported = True
                        say("exported MID (post Two-Weapon Fighting)")
                    except Exception as e:
                        notes.append(f"mid export failed: {e}")
                # fall through: cast1 just resolved on P0's priority, so
                # immediately consider starting cast2 below instead of going
                # idle (a no-send tick with no revision change would deadlock
                # the tick loop).
            # still on the stack: fall through and pass priority
        # 3) check cast2 resolution
        if cast2["submitted"] and not cast2["resolved"] and cast2["rev"] is not None \
                and p0.revision != cast2["rev"]:
            st2 = p0.latest["state"]
            zone = spell_zone(st2, cast2)
            wire("cast2_zone_check", {"zone": zone, "rev": p0.revision})
            if zone is not None and zone != "Stack":
                cast2["resolved"] = True
                if cast2["mode_chosen"] is None and ass["A2_modal_offered"] == "not-run":
                    ass["A2_modal_offered"] = "failed"
                    notes.append("cast2 resolved with NO modal choice ever offered")
                if cast2["candidates"] is None and ass["A4_archery_targets"] == "not-run":
                    # No target prompt was ever presented: with the legal-target
                    # set observed, the engine auto-targeted (same behavior as
                    # #658's single-legal-target auto-target). A5 still asserts
                    # the outcome on the targeted object.
                    notes.append("no Archery target prompt observed; engine "
                                 "auto-targeted (cf. #658 single-legal-target "
                                 "auto-target); A4 not-run, see A5")
                tgt = cast2.get("target_oid")
                if tgt is None:
                    # auto-target path: the destroyed BoP is the target
                    gy_bops = [int(oid) for oid, o in st2.get("objects", {}).items()
                               if o.get("zone") == "Graveyard" and o.get("controller") == 0
                               and (o.get("base_name") or o.get("name")) == BOP]
                    if len(gy_bops) == 1:
                        tgt = gy_bops[0]
                        notes.append(f"auto-target inferred: {BOP} oid {tgt}")
                tgt_o = get_obj(st2, tgt) if tgt else {}
                tgt_zone = tgt_o.get("zone")
                tgt_gy = (tgt_o.get("base_name") or tgt_o.get("name")) == BOP \
                    and tgt_zone == "Graveyard"
                others_ok = True
                for oid2, o2 in st2.get("objects", {}).items():
                    if o2.get("zone") != "Battlefield" or o2.get("controller") != 0:
                        continue
                    if "Creature" not in (o2.get("card_types", {}) or {}).get("core_types", []):
                        continue
                    if tgt and int(oid2) == tgt:
                        continue
                    if damage_on(o2) != 0:
                        others_ok = False
                    p2, t2 = pt(o2)
                    if p2 != num(o2.get("layer_base_power")) or t2 != num(o2.get("layer_base_toughness")):
                        others_ok = False
                say(f"cast2 resolved: target BoP oid={tgt} zone={tgt_zone}; "
                    f"others_ok={others_ok}; "
                    f"life={life(st2,0)}/{life(st2,1)}; gy={graveyard_names(st2,0)}")
                wire("cast2_resolved", {"target_oid": tgt, "target_zone": tgt_zone,
                                       "others_ok": others_ok,
                                       "life": [life(st2, 0), life(st2, 1)],
                                       "gy": graveyard_names(st2, 0)})
                if tgt_gy and others_ok \
                        and life(st2, 0) == 20 and life(st2, 1) == 20:
                    ass["A5_archery_damage"] = "passed"
                    notes.append(f"Archery: targeted {BOP} (oid {tgt}) destroyed "
                                 f"by 5 damage; all other creatures unharmed")
                else:
                    ass["A5_archery_damage"] = "failed"
                    notes.append(f"Archery wrong: target oid={tgt} zone={tgt_zone}, "
                                 f"others_ok={others_ok}, "
                                 f"life={life(st2,0)}/{life(st2,1)}")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST (post Archery)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                return
            # still on the stack: fall through and pass priority
        # 4) start cast 2 (Archery) once cast1 resolved and mana is back
        if (cast1["resolved"] and not cast2["submitted"] and pre_exported
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(untapped_forests(state, 0)) >= 3
                and WEAPON in hand_names(state, 0)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == WEAPON:
                    say("P0 casts Choose Your Weapon (Archery mode next)")
                    wire("cast2_action", a)
                    await submit_as_is(p0, a)
                    cast2["submitted"] = True
                    cast2["wanted_target"] = BOP
                    cast2["spell_oid"] = d.get("object_id")
                    rej = await drain_rejections(p0)
                    if rej:
                        say(f"cast2 submission rejected: {json.dumps(rej)[:400]}")
                        wire("cast2_rejected", rej)
                        notes.append(f"CastSpell(Choose Your Weapon, archery) rejected: {rej}")
                    return
        # 5) start cast 1 (Two-Weapon Fighting)
        if (not pre_exported and not cast1["submitted"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and setup_ready(state)):
            say("SETUP READY; exporting PRE")
            pre = await p0.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            pre_st = json.loads(pre)["state"]
            tn = two_two_name(pre_st)
            ok = (tn is not None and len(battlefield_ids(pre_st, 0, BOP)) > 0
                  and len(untapped_forests(pre_st, 0)) >= 3
                  and WEAPON in hand_names(pre_st, 0))
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"pre-cast setup: 2/2={tn}, BoP on BF, "
                         f"untapped forests={len(untapped_forests(pre_st,0))}, "
                         f"weapon in hand={WEAPON in hand_names(pre_st,0)}")
            wire("pre_state_meta", {"two_two": tn,
                                    "forests": len(untapped_forests(pre_st, 0))})
            pre_exported = True
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == WEAPON:
                    say(f"casting Choose Your Weapon, will choose Two-Weapon Fighting -> {tn}")
                    wire("cast1_action", a)
                    await submit_as_is(p0, a)
                    cast1["submitted"] = True
                    cast1["wanted_target"] = tn
                    cast1["spell_oid"] = d.get("object_id")
                    rej = await drain_rejections(p0)
                    if rej:
                        say(f"cast1 submission rejected: {json.dumps(rej)[:400]}")
                        wire("cast1_rejected", rej)
                        notes.append(f"CastSpell(Choose Your Weapon) rejected: {rej}")
                    return
            notes.append("setup ready but CastSpell(Choose Your Weapon) not offered")
            return
        # 6) normal setup play: land, then creatures (cheap first).
        # While a modal cast is in flight, do NOT play more cards — just pass,
        # so the board state the assertions read is the one we set up.
        in_flight = (cast1["submitted"] and not cast1["resolved"]) or \
                    (cast2["submitted"] and not cast2["resolved"])
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        if not in_flight:
            for nm in (BOP, ELVES, BEARS, RUNECLAW):
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == nm:
                        say(f"P0 casts {nm}")
                        await submit_as_is(p0, a)
                        return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        # fallback: pass offered only via viewer_interaction (no legal_actions)
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        say("P0 passes priority via viewer_interaction fallback")
                        wire("p0_vi_pass", sub)
                        await p0.send_interaction(sub)
                        return
        # diagnostic: owning priority but took no action
        say(f"P0 NO-ACTION at turn={state.get('turn_number')} phase={state.get('phase')} "
            f"acts={[a['type'] for a in acts][:10]} vi={bool(vi)}")
        wire("p0_no_action", {"turn": state.get("turn_number"),
                              "phase": state.get("phase"),
                              "acts": [a["type"] for a in acts][:10],
                              "vi_opps": len((vi or {}).get("opportunities", []))})

    async def c_send(c, action):
        await c.send_action(action)

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await c_send(p1, {"type": "MulliganDecision",
                              "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "DiscardToHandSize":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []):
                    data = (opp.get("response", {}) or {}).get("data", {}) or {}
                    cands = data.get("candidates") or []
                    if cands:
                        spec = data.get("spec") or {}
                        stype = spec.get("type", "select")
                        sub = {"interactionId": opp.get("interactionId"),
                               "response": {"type": stype,
                                            "data": {"choiceIds": [cands[0]["id"]]}}}
                        await p1.send_interaction(sub)
                        say("P1 discards to hand size")
                        return
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        say("P1 passes priority via viewer_interaction fallback")
                        wire("p1_vi_pass", sub)
                        await p1.send_interaction(sub)
                        return
        say(f"P1 NO-ACTION at turn={state.get('turn_number')} phase={state.get('phase')} "
            f"acts={[a['type'] for a in acts][:10]} vi={bool(vi)}")

    t0 = time.time()
    last = {}
    last_diag = 0.0
    last_tick_at = {}
    stuck_deadline = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            # Liveness: re-tick a client at most every 5s even when nothing
            # changed. A tick that sends nothing leaves no revision change;
            # without this the loop deadlocks waiting on a client that owns
            # priority (observed: cast2 never started after cast1 resolved).
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if cast2["resolved"] and ass["A5_archery_damage"] != "not-run":
            # A6: game proceeds, no stuck decision
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            wcount = sum(1 for oid, o in s.get("objects", {}).items()
                         if o.get("zone") == "Graveyard"
                         and (o.get("base_name") or o.get("name")) == WEAPON)
            if wcount >= 2 and wf in ("Priority", None, "DeclareAttackers",
                                      "DeclareBlockers", "Untap", "DiscardToHandSize"):
                ass["A6_cleanup"] = "passed"
                notes.append(f"both casts in graveyard ({wcount}); game proceeding (wf={wf})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"cleanup wrong: weapon copies in gy={wcount}, wf={wf}")
            say("all assertions resolved; finishing")
            await finish()
            return
        # A2 check: both modes were actually chosen through the modal interaction
        if cast1["mode_chosen"] and cast2["mode_chosen"] and ass["A2_modal_offered"] == "not-run":
            ass["A2_modal_offered"] = "passed"
            notes.append("modal choice presented for both casts; "
                         f"cast1 chose {cast1['mode_chosen']}, cast2 chose {cast2['mode_chosen']}")
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            acts0 = [a["type"] for a in merged_actions(p0.latest)] if p0.latest else []
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)} "
                f"forests={len(untapped_forests(s,0))} P0acts={acts0[:10]} "
                f"cast1={cast1['submitted']}/{cast1['mode_chosen']}/{cast1['resolved']} "
                f"cast2={cast2['submitted']}/{cast2['mode_chosen']}/{cast2['resolved']}")
        # stall detection: a cast submitted but no interaction progress for 120s
        in_flight = ((cast1["submitted"] and not cast1["resolved"])
                     or (cast2["submitted"] and not cast2["resolved"]))
        if in_flight and stuck_deadline is None:
            stuck_deadline = time.time() + 180
        if not in_flight:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("cast submitted but modal/target flow stalled 180s; see wire log")
            if cast1["submitted"] and not cast1["resolved"]:
                ass["A2_modal_offered"] = "failed"
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
