#!/usr/bin/env python3
"""Issue #5474: "Stuck decision: ModalFaceChoice" (+ sibling #5487 Tergrid).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report #5474 (2026-07-10): "Tried to play Tony Stark commander and it got stuck
when asking to select which side to play cast." Diagnostic: build v0.20.0
(4b5af92), Waiting for: ModalFaceChoice, Stuck players: 1.
Sibling #5487 (2026-07-10): "I tried to summon Tergrid, God of Fright, and the
game wouldn't let me select either face of the card." Stuck players: 0.
Classifier (2026-07-19, both): not_card_data_attributable - the face choice is
a modal-choice routing/UI state problem, not a card-text parser defect.

Oracle (verified from pinned v0.78.0 card-data.json):
  "tony stark" front {1}{U} Legendary Creature - Human Artificer Hero,
    layout modal_dfc, face_index 0; back face "iron man, tony stark" {3}{R}{R}.
  "tergrid, god of fright" front {3}{B}{B} Legendary Creature - God,
    layout modal_dfc, face_index 0; back face "tergrid's lantern" {3}{B}
    Legendary Artifact.
Per CR 712.8a the player casting a modal double-faced card chooses which face
to cast, so both faces must be selectable at the ModalFaceChoice decision.

Scenario (native engine, two human-client seats; commander seat not required to
exercise the ModalFaceChoice decision type):
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
                     face choice and an actionable submission (viewer
                     canSubmit or ChooseModalFace legal action).
  A3_choice_accepted The advertised front-face choice submits with no
                     rejection and the cast advances (no >60s stall).
  A4_front_completes post_front.json: Tony Stark on P0 battlefield, stack
                     empty, game advanced past the cast turn, life 20/20.
  A5_back_reachable  The back face ("iron man, tony stark") is offered as a
                     choice at some ModalFaceChoice, or the exploratory
                     back_face=true submission is accepted.
  A6_cleanup         post.json: stack empty, waiting_for Priority, game
                     proceeding.
  B1..B4 (Tergrid): setup / choice offered / choice accepted / cast completes.

Verdict rule: reproduced iff A2 fails (no actionable face choice), A3 fails
(rejection/stall), or A5 fails (back face never selectable - the same
"select which side" decision broken on this build; differences from the
v0.20.0 hard stall are explained). not-reproduced iff A1-A6 and B1-B4 pass.
blocked iff the game cannot be driven to a ModalFaceChoice.

Evidence: evidence/5474/<run-id>/pre.json, post_front.json, pre_cast2.json,
post.json, modalfacechoice_*.json, g2_pre.json, g2_post.json, run.json,
manifest.sha256, summary.png, scenario_5474.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _c
_c.URL = "ws://127.0.0.1:9375/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-5474"
EVDIR = f"{BACKFILL}/evidence/5474/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TONY = "tony stark"
IRONMAN = "iron man, tony stark"
TERGRID = "tergrid, god of fright"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
SWAMP = "swamp"

P0_DECK_G1 = [(TONY, 12), (ISLAND, 24), (MOUNTAIN, 24)]
P1_DECK_G1 = [(FOREST, 60)]
P0_DECK_G2 = [(TERGRID, 12), (SWAMP, 48)]
P1_DECK_G2 = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello probed live (0.78.0/4de7224/proto 68/Full) + sha256 "
              "re-verified against pinned v0.78.0 release artifacts "
              "(binary+data+sigs under server/releases/v0.78.0/); fresh "
              "isolated server on 127.0.0.1:9375 for run 20260909-5474",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name and not o.get("tapped")]


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def on_bf(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def stack_names(state):
    out = []
    for e in state.get("stack", []) or []:
        if isinstance(e, dict):
            oid = e.get("object_id") or e.get("id")
            out.append(lname(state, oid) if oid else json.dumps(e)[:80])
        else:
            out.append(str(e)[:80])
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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def face_value_of_choice(ch):
    """Extract the advertised face ('front'/'back'/...) from an exactChoices
    choice candidate."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {}) if isinstance(s.get("data"), dict) else {}
        if d.get("role") == "face" and d.get("value"):
            return str(d["value"])
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {}) if isinstance(s.get("data"), dict) else {}
        if isinstance(d.get("code"), str) and "face" in d["code"].lower():
            return d["code"]
    return None

async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_modal_offered", "A3_choice_accepted",
            "A4_front_completes", "A5_back_reachable", "A6_cleanup",
            "B1_tergrid_setup", "B2_tergrid_offered", "B3_tergrid_accepted",
            "B4_tergrid_completes")}
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    clients = {"P0": p0, "P1": p1}

    obs = {
        "game": 1,
        "stage": "g1_setup",
        "kept": {},
        "cast1_submitted": False,
        "cast1_oid": None,
        "cast1_turn": None,
        "face1_opps": [],        # recorded ModalFaceChoice opportunities (cast 1)
        "face1_choice_id": None,
        "face1_submitted": False,
        "face1_rejected": False,
        "face1_stall": False,
        "face1_wait_start": None,
        "tony1_on_bf": False,
        "cast2_submitted": False,
        "cast2_oid": None,
        "face2_opps": [],
        "face2_choice_id": None,
        "face2_submitted": False,
        "face2_rejected": False,
        "back_probe_sent": False,
        "back_probe_accepted": None,   # None=not-run, True/False
        "back_probe_rejection": None,
        "ironman_on_bf": False,
        "tony2_on_bf": False,
        "g1_cast_spells_seen": [],
        # game 2
        "g2_cast_submitted": False,
        "g2_cast_oid": None,
        "g2_opps": [],
        "g2_choice_id": None,
        "g2_submitted": False,
        "g2_rejected": False,
        "g2_wait_start": None,
        "g2_stall": False,
        "tergrid_on_bf": False,
        "rejections": [],
        "cast_spell_ads": [],    # all CastSpell actions advertised for the MDFC
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    async def export(tag):
        # only the host (P0) may ExportAuthoritativeState
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            say(f"export {tag} FAILED: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    def record_modal_opp(st, state, tag):
        """Record the full ModalFaceChoice opportunity for evidence."""
        wf = state.get("waiting_for") or {}
        vi = st.get("viewer_interaction") or {}
        snap = {
            "waiting_for": wf,
            "viewer_interaction": vi,
            "legal_actions": st.get("legal_actions"),
            "legal_actions_by_object": st.get("legal_actions_by_object"),
        }
        with open(f"{EVDIR}/modalfacechoice_{tag}.json", "w") as f:
            json.dump(snap, f, indent=1, default=str)
        opps = (vi.get("opportunities") or []) if vi.get("canSubmit") else []
        faces = []
        for o in opps:
            resp = o.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            for ch in data.get("choices") or data.get("candidates") or []:
                faces.append({
                    "id": ch.get("id"),
                    "face": face_value_of_choice(ch),
                    "status": (ch.get("status") or {}).get("type"),
                })
        rec = {
            "tag": tag,
            "interaction_ids": [o.get("interactionId") for o in opps],
            "response_types": [ (o.get("response") or {}).get("type") for o in opps],
            "faces": faces,
            "canSubmit": vi.get("canSubmit"),
            "choose_modal_face_actions": [
                a.get("data") for a in merged_actions(st)
                if a["type"] == "ChooseModalFace"],
        }
        wire("modal_opp", rec)
        say(f"ModalFaceChoice [{tag}]: faces={faces} "
            f"canSubmit={vi.get('canSubmit')} "
            f"legalChooseModalFace={rec['choose_modal_face_actions']}")
        return rec

    async def answer_modal_face(c, st, rec, tag, opp_list_key, choice_key,
                                submitted_key):
        """Submit the advertised front-face choice via the viewer_interaction
        exactChoices opportunity (preferred) or the ChooseModalFace action."""
        vi = st.get("viewer_interaction") or {}
        opps = (vi.get("opportunities") or []) if vi.get("canSubmit") else []
        for o in opps:
            resp = o.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            if resp.get("type") != "exactChoices":
                continue
            for ch in data.get("choices") or []:
                if (ch.get("status") or {}).get("type") != "available":
                    continue
                if face_value_of_choice(ch) == "front":
                    sub = {"interactionId": o.get("interactionId"),
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch.get("id")}}}
                    obs[choice_key] = ch.get("id")
                    wire("face_choice_submission",
                         {"who": c.name, "tag": tag, "submission": sub})
                    say(f"[{c.name}] submits face choice front "
                        f"({ch.get('id')}) via Interaction")
                    await c.send_interaction(sub)
                    obs[submitted_key] = True
                    return True
        # fallback: legacy ChooseModalFace action
        for a in merged_actions(st):
            if a["type"] == "ChooseModalFace":
                wire("face_choice_action_fallback",
                     {"who": c.name, "tag": tag, "action": a})
                say(f"[{c.name}] fallback: ChooseModalFace action as-is")
                await submit_as_is(c, a)
                obs[submitted_key] = True
                return True
        say(f"[{c.name}] NO actionable face choice at {tag}")
        return False

    def mulligan_tick(c, pid, st, acts, state, want_card, land_names):
        key = f"{c.name}_g{obs['game']}"
        ma = find_action(acts, "MulliganDecision")
        if ma and not obs["kept"].get(key):
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n in land_names)
            mulls = obs["kept"].get(key + "_m", 0)
            want_ok = (want_card in hn) if want_card else True
            if (want_ok and lands >= 2) or mulls >= 3:
                obs["kept"][key] = True
                return ("keep", want_ok, lands, mulls)
            obs["kept"][key + "_m"] = mulls + 1
            return ("mull", want_ok, lands, mulls)
        return None

    async def do_mulligan(c, pid, st, acts, state, decision):
        key = f"{c.name}_g{obs['game']}"
        if decision[0] == "keep":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{c.name}] keeps (want_ok={decision[1]}, lands={decision[2]}, "
                f"mulls={decision[3]})")
        else:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{c.name}] mulligans #{decision[3] + 1}")
        return True

    async def bottom_cards(c, pid, st, acts, state, land_names, keep_names):
        key = f"{c.name}_g{obs['game']}_b"
        sc = find_action(acts, "SelectCards")
        if sc and not obs["kept"].get(key):
            obs["kept"][key] = True
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            hand_ids = [o for o in player_of(state, pid).get("hand", [])]

            def bkey(oid):
                nm = lname(state, oid)
                if nm in land_names:
                    return 0
                return 2 if nm in keep_names else 1
            picks = sorted(hand_ids, key=bkey)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"[{c.name}] bottoms {count}")
        return True

    async def generic_tick(c, pid, st, acts, state):
        """Mulligan, payments, combat declarations, triggers; returns True if
        it consumed the tick."""
        wtype = (state.get("waiting_for") or {}).get("type")
        land_names = (ISLAND, MOUNTAIN, FOREST) if obs["game"] == 1 else (SWAMP, FOREST)
        want = TONY if (obs["game"] == 1 and pid == 0) else (
            TERGRID if (obs["game"] == 2 and pid == 0) else None)
        dec = mulligan_tick(c, pid, st, acts, state, want, land_names)
        if dec:
            await do_mulligan(c, pid, st, acts, state, dec)
            return True
        if wtype == "MulliganDecision":
            keep_names = [TONY] if obs["game"] == 1 else [TERGRID]
            await bottom_cards(c, pid, st, acts, state, land_names, keep_names)
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return True
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
            return True
        if wtype == "ChooseLegend":
            # legend rule after the second Tony Stark resolves; keep one
            vi = get_vi(st)
            if vi:
                for o in vi.get("opportunities", []) or []:
                    resp = o.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    if resp.get("type") == "exactChoices":
                        for ch in data.get("choices") or []:
                            if (ch.get("status") or {}).get("type") == "available":
                                sub = {"interactionId": o.get("interactionId"),
                                       "response": {"type": "choose",
                                                    "data": {"choiceId": ch.get("id")}}}
                                wire("legend_choice",
                                     {"who": c.name, "submission": sub})
                                say(f"[{c.name}] legend rule: keep {ch.get('id')}")
                                await c.send_interaction(sub)
                                return True
            la = find_action(acts, "ChooseLegend")
            if la:
                wire("legend_action", {"who": c.name, "action": la})
                say(f"[{c.name}] legend rule: action as-is")
                await submit_as_is(c, la)
                return True
            return True
        return False

    async def p0_tick(st, acts, state):
        if await generic_tick(p0, 0, st, acts, state):
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        # ---- ModalFaceChoice handling (both games) ----
        if wtype == "ModalFaceChoice":
            if obs["game"] == 1 and obs["stage"] == "g1_face1" \
                    and not obs["face1_submitted"]:
                rec = record_modal_opp(st, state, "g1_cast1")
                obs["face1_opps"].append(rec)
                obs["face1_wait_start"] = obs["face1_wait_start"] or time.time()
                if not await answer_modal_face(p0, st, rec, "g1_cast1",
                                               "face1_opps", "face1_choice_id",
                                               "face1_submitted"):
                    return True  # no actionable choice; watchdog in main loop
                return True
            if obs["game"] == 1 and obs["stage"] == "g1_face2" \
                    and not obs["face2_submitted"]:
                rec = record_modal_opp(st, state, "g1_cast2")
                obs["face2_opps"].append(rec)
                # exploratory: is the back face reachable at all? (constructed
                # submission, logged as exploratory)
                if not obs["back_probe_sent"]:
                    obs["back_probe_sent"] = True
                    rej_mark = len(obs["rejections"])
                    obs["_back_rej_mark"] = rej_mark
                    sub = {"type": "ChooseModalFace",
                           "data": {"back_face": True}}
                    wire("back_face_probe", {"submission": sub,
                                             "note": "constructed, not advertised"})
                    say("[P0] exploratory: ChooseModalFace back_face=true")
                    await p0.send_action(sub)
                    return True
                if not await answer_modal_face(p0, st, rec, "g1_cast2",
                                               "face2_opps", "face2_choice_id",
                                               "face2_submitted"):
                    return True
                return True
            if obs["game"] == 2 and obs["stage"] == "g2_face" \
                    and not obs["g2_submitted"]:
                rec = record_modal_opp(st, state, "g2_cast1")
                obs["g2_opps"].append(rec)
                obs["g2_wait_start"] = obs["g2_wait_start"] or time.time()
                if not await answer_modal_face(p0, st, rec, "g2_cast1",
                                               "g2_opps", "g2_choice_id",
                                               "g2_submitted"):
                    return True
                return True
            return True  # ModalFaceChoice owned by this client but not our stage
        if wtype != "Priority" or state.get("priority_player") != 0:
            return False
        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return True
            if obs["game"] == 1 and obs["stage"] == "g1_setup" \
                    and not obs["cast1_submitted"]:
                if len(untapped_lands(state, 0, ISLAND)) >= 1 \
                        and len(untapped_lands(state, 0, ISLAND)
                                + untapped_lands(state, 0, MOUNTAIN)) >= 2:
                    sid = spell_in_hand_oid(state, 0, TONY)
                    cands = [a for a in acts
                             if a["type"] == "CastSpell"
                             and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
                    obs["g1_cast_spells_seen"] = [
                        {"object_id": a["data"].get("object_id"),
                         "data_keys": sorted((a.get("data") or {}).keys())}
                        for a in acts if a["type"] == "CastSpell"]
                    if sid is not None and cands:
                        obs["cast1_submitted"] = True
                        obs["cast1_oid"] = sid
                        obs["cast1_turn"] = state.get("turn_number")
                        say(f"P0 casts Tony Stark oid={sid} "
                            f"(castspell variants seen: {len(cands)})")
                        wire("cast1", {"action": cands[0],
                                       "all_cast_spells": obs["g1_cast_spells_seen"]})
                        await export("pre")
                        await submit_as_is(p0, cands[0])
                        return True
            if obs["game"] == 1 and obs["stage"] == "g1_face2_prep" \
                    and not obs["cast2_submitted"]:
                mts = untapped_lands(state, 0, MOUNTAIN)
                allu = (untapped_lands(state, 0, ISLAND)
                        + untapped_lands(state, 0, MOUNTAIN))
                if len(mts) >= 2 and len(allu) >= 5:
                    sid = spell_in_hand_oid(state, 0, TONY)
                    cands = [a for a in acts
                             if a["type"] == "CastSpell"
                             and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
                    if sid is not None and cands:
                        obs["cast2_submitted"] = True
                        obs["cast2_oid"] = sid
                        say(f"P0 casts second Tony Stark oid={sid} "
                            f"with {len(allu)} untapped lands ({len(mts)} mountains)")
                        wire("cast2", {"action": cands[0]})
                        await export("pre_cast2")
                        await submit_as_is(p0, cands[0])
                        return True
            if obs["game"] == 2 and obs["stage"] == "g2_setup" \
                    and not obs["g2_cast_submitted"]:
                if len(untapped_lands(state, 0, SWAMP)) >= 5:
                    sid = spell_in_hand_oid(state, 0, TERGRID)
                    cands = [a for a in acts
                             if a["type"] == "CastSpell"
                             and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
                    if sid is not None and cands:
                        obs["g2_cast_submitted"] = True
                        obs["g2_cast_oid"] = sid
                        say(f"P0 casts Tergrid oid={sid}")
                        wire("g2_cast", {"action": cands[0]})
                        await export("g2_pre")
                        await submit_as_is(p0, cands[0])
                        return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        if await generic_tick(p1, 1, st, acts, state):
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "ModalFaceChoice":
            return True  # not P1's decision; hold
        if wtype != "Priority" or state.get("priority_player") != 1:
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        pre = load_env("pre")
        postF = load_env("post_front")
        post = load_env("post")
        g2pre = load_env("g2_pre")
        g2post = load_env("g2_post")
        notes.append(f"rejections={len(obs['rejections'])} "
                     f"face1_opps={len(obs['face1_opps'])} "
                     f"face2_opps={len(obs['face2_opps'])} "
                     f"g2_opps={len(obs['g2_opps'])} "
                     f"back_probe_accepted={obs['back_probe_accepted']}")
        # A1
        if pre is not None:
            s = pre["state"]
            tony_in_hand = any(
                lname(s, o) == TONY for o in player_of(s, 0).get("hand", []))
            ok = (s.get("active_player") == 0
                  and s.get("phase") == "PreCombatMain"
                  and tony_in_hand
                  and len(untapped_lands(s, 0, ISLAND)) >= 1)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: active={s.get('active_player')} phase={s.get('phase')} "
                         f"tony_in_hand={tony_in_hand} "
                         f"untapped_islands={len(untapped_lands(s, 0, ISLAND))}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing (first cast never submitted)")
        # A2
        recs = obs["face1_opps"]
        if not recs:
            ass["A2_modal_offered"] = "failed"
            notes.append("A2: no ModalFaceChoice wait observed for cast 1")
        else:
            r = recs[0]
            faces = [f["face"] for f in r["faces"]]
            actionable = (r["canSubmit"] and len(r["faces"]) > 0) \
                or len(r["choose_modal_face_actions"]) > 0
            if actionable:
                ass["A2_modal_offered"] = "passed"
            else:
                ass["A2_modal_offered"] = "failed"
            notes.append(f"A2: ModalFaceChoice wait seen; faces advertised={faces}; "
                         f"canSubmit={r['canSubmit']}; "
                         f"chooseModalFace actions={r['choose_modal_face_actions']}; "
                         f"actionable={actionable}")
        # A3
        if not obs["face1_submitted"]:
            ass["A3_choice_accepted"] = "not-run"
            notes.append("A3: no face choice was submitted")
        elif obs["face1_rejected"]:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: advertised front-face choice was REJECTED")
        elif obs["face1_stall"]:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: ModalFaceChoice stalled >60s with no actionable "
                         "submission (the reported stuck signature)")
        elif obs["tony1_on_bf"]:
            ass["A3_choice_accepted"] = "passed"
            notes.append("A3: front-face choice accepted; cast resolved to "
                         "Tony Stark on the battlefield")
        else:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: choice submitted but cast never resolved")
        # A4
        if postF is not None:
            s = postF["state"]
            tony_bf = on_bf(s, 0, TONY)
            stack_empty = len(s.get("stack", []) or []) == 0
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
        for r in obs["face1_opps"] + obs["face2_opps"]:
            for f in r["faces"]:
                if f["face"]:
                    offered_faces.add(f["face"])
        if not obs["cast2_submitted"]:
            ass["A5_back_reachable"] = "not-run"
            notes.append("A5: second cast never submitted; back-face "
                         "reachability not tested")
        elif "back" in offered_faces or obs["back_probe_accepted"] is True:
            ass["A5_back_reachable"] = "passed"
            notes.append(f"A5: back face reachable "
                         f"(offered faces={sorted(offered_faces)}, "
                         f"probe accepted={obs['back_probe_accepted']})")
        else:
            ass["A5_back_reachable"] = "failed"
            notes.append(f"A5: back face NEVER offered at any ModalFaceChoice "
                         f"(offered faces={sorted(offered_faces)}); exploratory "
                         f"back_face=true submission "
                         f"{'rejected' if obs['back_probe_accepted'] is False else 'not-run'}; "
                         f"per CR 712.8a the caster chooses which face to cast, "
                         f"so Iron Man, Tony Stark ({{3}}{{R}}{{R}}) is unreachable")
        # A6
        if post is not None:
            s = post["state"]
            stack_empty = len(s.get("stack", []) or []) == 0
            wf = (s.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A6_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A6: post.json stack_empty={stack_empty} wf={wf} "
                         f"turn={s.get('turn_number')} phase={s.get('phase')}")
        else:
            ass["A6_cleanup"] = "not-run"
            notes.append("A6: post.json missing")
        # B (Tergrid)
        if g2pre is not None:
            s = g2pre["state"]
            ok = any(lname(s, o) == TERGRID
                     for o in player_of(s, 0).get("hand", []))
            ass["B1_tergrid_setup"] = "passed" if ok else "failed"
        else:
            ass["B1_tergrid_setup"] = "failed"
            notes.append("B1: g2_pre.json missing")
        if obs["g2_opps"]:
            r = obs["g2_opps"][0]
            actionable = (r["canSubmit"] and len(r["faces"]) > 0) \
                or len(r["choose_modal_face_actions"]) > 0
            ass["B2_tergrid_offered"] = "passed" if actionable else "failed"
            notes.append(f"B2: Tergrid ModalFaceChoice faces="
                         f"{[f['face'] for f in r['faces']]} actionable={actionable}")
        else:
            ass["B2_tergrid_offered"] = "failed"
            notes.append("B2: no ModalFaceChoice wait in game 2")
        if obs["g2_stall"]:
            ass["B3_tergrid_accepted"] = "failed"
            notes.append("B3: ModalFaceChoice stalled >60s with no actionable "
                         "submission (the reported stuck signature)")
        elif not obs["g2_submitted"]:
            ass["B3_tergrid_accepted"] = "not-run"
        elif obs["g2_rejected"]:
            ass["B3_tergrid_accepted"] = "failed"
            notes.append("B3: Tergrid face choice rejected")
        elif obs["tergrid_on_bf"]:
            ass["B3_tergrid_accepted"] = "passed"
        else:
            ass["B3_tergrid_accepted"] = "failed"
            notes.append("B3: Tergrid choice submitted but never resolved")
        if g2post is not None:
            s = g2post["state"]
            tg = on_bf(s, 0, TERGRID)
            ok = len(tg) > 0 and len(s.get("stack", []) or []) == 0
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
            notes.append("RELATED FAILURE: the v0.20.0 hard stall is gone, but the "
                         "ModalFaceChoice offers only the front face - the back face "
                         "is never selectable, so the 'select which side' decision "
                         "remains broken on this build")
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
        return verdict

    async def finish():
        dur = time.time() - t_start
        for tag in ("post_front", "post", "g2_post"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": 5474,
            "sibling_issue": 5487,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9375,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5474.py", "rb").read()).hexdigest(),
            "decks": {"G1P0": P0_DECK_G1, "G1P1": P1_DECK_G1,
                      "G2P0": P0_DECK_G2, "G2P1": P1_DECK_G2},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if not k.startswith("_")},
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x MDFC / high land density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The reporter cast Tony Stark as a commander; the scenario casts it "
                "from the main deck. The ModalFaceChoice decision type is identical.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
                "The back_face=true probe submission was constructed (not engine-"
                "advertised) and is logged as exploratory.",
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
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    # copy scenario into evidence dir for provenance
    with open(f"{EVDIR}/scenario_5474.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_5474.py").read())

    # ---- game 1 ----
    await p0.connect()
    await p0.create(deck(*P0_DECK_G1))
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK_G1))
    say(f"G1 game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    async def run_loop(game_no, max_turns):
        t0 = time.time()
        last = {}
        last_tick_at = {}
        last_diag = 0.0
        while time.time() - t0 < 1500:
            await asyncio.sleep(0.15)
            for c, tick in ((p0, p0_tick), (p1, p1_tick)):
                await drain_rejections(c)
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                same_rev = (rev == last.get(c.name))
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
            # back-face probe result detection (g1 cast 2)
            if obs["back_probe_sent"] and obs["back_probe_accepted"] is None:
                mark = obs.get("_back_rej_mark", 0)
                new_rej = [r for r in obs["rejections"][mark:]
                           if r["who"] == "P0"]
                if new_rej:
                    obs["back_probe_accepted"] = False
                    obs["back_probe_rejection"] = new_rej[-1]["data"]
                    wire("back_probe_result", {"accepted": False,
                                               "rejection": new_rej[-1]["data"]})
                    say("back_face=true REJECTED: "
                        f"{json.dumps(new_rej[-1]['data'])[:300]}")
            s = (p0.latest["state"] if p0.latest else {}) or {}
            turn = s.get("turn_number", 0)
            phase = s.get("phase")
            active = s.get("active_player")
            wtype = (s.get("waiting_for") or {}).get("type")
            # ---- stage transitions ----
            if game_no == 1:
                if obs["stage"] == "g1_setup" and obs["cast1_submitted"]:
                    obs["stage"] = "g1_face1"
                    say("STAGE -> g1_face1")
                elif obs["stage"] == "g1_face1":
                    if not obs["tony1_on_bf"] and on_bf(s, 0, TONY):
                        obs["tony1_on_bf"] = True
                        await export("post_front")
                        obs["stage"] = "g1_face2_prep"
                        say("STAGE -> g1_face2_prep (Tony Stark #1 on BF)")
                elif obs["stage"] == "g1_face2_prep" and obs["cast2_submitted"]:
                    obs["stage"] = "g1_face2"
                    say("STAGE -> g1_face2")
                elif obs["stage"] == "g1_face2":
                    if on_bf(s, 0, IRONMAN):
                        obs["ironman_on_bf"] = True
                    if len(on_bf(s, 0, TONY)) >= 2:
                        obs["tony2_on_bf"] = True
                    if (obs["ironman_on_bf"] or obs["tony2_on_bf"]) \
                            and len(s.get("stack", []) or []) == 0 \
                            and (s.get("waiting_for") or {}).get("type") == "Priority":
                        await export("post")
                        say("g1 cast2 resolved; game 1 done")
                        return True
            else:
                if obs["stage"] == "g2_setup" and obs["g2_cast_submitted"]:
                    obs["stage"] = "g2_face"
                    say("STAGE -> g2_face")
                elif obs["stage"] == "g2_face":
                    if not obs["tergrid_on_bf"] and on_bf(s, 0, TERGRID):
                        obs["tergrid_on_bf"] = True
                        await export("g2_post")
                        say("game 2 done (Tergrid on BF)")
                        return True
            # ModalFaceChoice stall watchdog (the reported stuck signature)
            for key, start_key in (("face1", "face1_wait_start"),
                                  ("g2", "g2_wait_start")):
                ws = obs.get(start_key)
                submitted = obs.get(f"{key}_submitted")
                if ws and not submitted and time.time() - ws > 60:
                    rec_key = f"{key}_opps"
                    actionable = any(
                        (r["canSubmit"] and r["faces"])
                        or r["choose_modal_face_actions"]
                        for r in obs[rec_key])
                    if not actionable:
                        obs[f"{key}_stall"] = True
                        await export("mid_stall")
                        say(f"STALL: ModalFaceChoice >60s with no actionable "
                            f"submission ({key}); captured mid_stall.json")
                        return False
            if turn >= max_turns:
                notes.append(f"game {game_no}: turn {max_turns} reached; bailing out")
                say(f"game {game_no} turn bail; moving on")
                return False
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                say(f"DIAG g{game_no} turn={turn} active={active} phase={phase} "
                    f"wf={wtype} stage={obs['stage']} "
                    f"p0hand={hand_names(s,0)[:6]} "
                    f"tony_bf={on_bf(s,0,TONY)} stack={stack_names(s)[:3]}")

    ok1 = await run_loop(1, 25)
    say(f"game 1 loop ended ok={ok1}")

    # ---- game 2 (Tergrid, sibling #5487) ----
    # fresh sockets: a socket already attached to a game session cannot create
    try:
        await p0.close()
    except Exception:
        pass
    try:
        await p1.close()
    except Exception:
        pass
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    obs["game"] = 2
    obs["stage"] = "g2_setup"
    obs["kept"] = {}
    await p0.connect()
    await p0.create(deck(*P0_DECK_G2))
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK_G2))
    say(f"G2 game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ok2 = await run_loop(2, 25)
    say(f"game 2 loop ended ok={ok2}")

    await finish()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
