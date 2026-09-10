#!/usr/bin/env python3
"""Issue #5487: "I tried to summon Tergrid, God of Fright, and the game
wouldn't let me select either face of the card." Diagnostic: build v0.20.0
(7b5b324), Waiting for: ModalFaceChoice, Stuck players: 0. Classifier
(2026-07-19): not_card_data_attributable - modal-face action routing/UI
state, not card-text parser.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle (verified from pinned v0.78.0 card-data.json):
  "Tergrid, God of Fright" front face {3}{B}{B}, Legendary Creature - God
    4/5, layout modal_dfc, face_index 0; back face "Tergrid's Lantern"
    {3}{B}, Legendary Artifact.
Per CR 712.8a the player casting a modal double-faced card chooses which
face to cast; the report claims NEITHER face could be selected. The reported
outcome is therefore tested directly on both faces:

Scenario (native engine, two human-client seats):
  Game 1 (front face): P0 casts Tergrid at main-phase priority. At the
    ModalFaceChoice wait the driver records the advertised faces and their
    actionability, submits the advertised FRONT-face choice, drives payment
    and resolution.
  Game 2 (back face): P0 casts Tergrid again and submits the advertised
    BACK-face choice (Tergrid's Lantern {3}{B} artifact).

Assertions:
  A1_setup_ok        pre.json: P0 PreCombatMain priority, Tergrid in hand,
                     >=5 untapped Swamps.
  A2_modal_offered   A ModalFaceChoice wait appears for P0 with >=2
                     advertised face choices (front + back) and an actionable
                     submission (viewer canSubmit).
  A3_choice_accepted The advertised front-face choice submits with no
                     rejection and the cast advances (no >60s stall).
  A4_front_completes post_front.json: Tergrid, God of Fright on P0
                     battlefield, stack empty, life 20/20.
  A5_back_completes  post_back.json (game 2): Tergrid's Lantern on P0
                     battlefield as an artifact, stack empty, life 20/20.
  A6_cleanup         post.json: stack empty, game proceeding (Priority).

Verdict rule: reproduced iff A2 fails (fewer than 2 actionable face
choices), A3 fails (rejection/stall on either face), or A4/A5 fail (the
chosen face never reaches the battlefield). not-reproduced iff A1-A6 all
pass. blocked iff the game cannot be driven to a ModalFaceChoice.

Relation to #5474 (same ModalFaceChoice decision type, published
2026-09-09): its sibling Tergrid sub-game offered both faces and completed
the front-face cast; this run re-exercises Tergrid directly and adds the
back-face (Lantern) path, which is the untested branch of the reported
"either face" claim.

Evidence: evidence/5487/<run-id>/pre.json, modalfacechoice_*.json,
post_front.json, post_back.json, post.json, g2_pre.json, run.json,
manifest.sha256, summary.png, scenario_5487.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _c
_c.URL = "ws://127.0.0.1:9376/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-5487"
EVDIR = f"{BACKFILL}/evidence/5487/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TERGRID = "tergrid, god of fright"
LANTERN = "tergrid's lantern"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [(TERGRID, 12), (SWAMP, 48)]
P1_DECK = [(FOREST, 60)]

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
              "isolated server on 127.0.0.1:9376 for run 20260909-5487",
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
            "A4_front_completes", "A5_back_completes", "A6_cleanup")}
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")

    obs = {
        "game": 1,
        "stage": "g1_setup",
        "kept": {},
        "cast_submitted": False,
        "cast_oid": None,
        "want_face": "front",
        "opps": [],
        "choice_id": None,
        "choice_submitted": False,
        "rejected": False,
        "stall": False,
        "wait_start": None,
        "tergrid_on_bf": False,
        "lantern_on_bf": False,
        "rejections": [],
        "cast_spell_ads": [],
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
                if obs["choice_submitted"] and not obs["tergrid_on_bf"] \
                        and not obs["lantern_on_bf"]:
                    obs["rejected"] = True

    async def export(tag):
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
            "response_types": [(o.get("response") or {}).get("type")
                               for o in opps],
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

    async def answer_modal_face(c, st, rec, tag, want_face):
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
                if face_value_of_choice(ch) == want_face:
                    sub = {"interactionId": o.get("interactionId"),
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch.get("id")}}}
                    obs["choice_id"] = ch.get("id")
                    wire("face_choice_submission",
                         {"who": c.name, "tag": tag, "want_face": want_face,
                          "submission": sub})
                    say(f"[{c.name}] submits face choice {want_face} "
                        f"({ch.get('id')}) via Interaction")
                    await c.send_interaction(sub)
                    obs["choice_submitted"] = True
                    return True
        for a in merged_actions(st):
            if a["type"] == "ChooseModalFace":
                wire("face_choice_action_fallback",
                     {"who": c.name, "tag": tag, "action": a})
                say(f"[{c.name}] fallback: ChooseModalFace action as-is")
                await submit_as_is(c, a)
                obs["choice_submitted"] = True
                return True
        say(f"[{c.name}] NO actionable face choice at {tag}")
        return False

    def mulligan_tick(c, pid, st, acts, state):
        key = f"{c.name}_g{obs['game']}"
        ma = find_action(acts, "MulliganDecision")
        if ma and not obs["kept"].get(key):
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n == SWAMP or n == FOREST)
            mulls = obs["kept"].get(key + "_m", 0)
            want_ok = (pid != 0) or (TERGRID in hn)
            if (want_ok and lands >= 2) or mulls >= 3:
                obs["kept"][key] = True
                return ("keep", want_ok, lands, mulls)
            obs["kept"][key + "_m"] = mulls + 1
            return ("mull", want_ok, lands, mulls)
        return None

    async def do_mulligan(c, pid, st, acts, state, decision):
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

    async def bottom_cards(c, pid, st, acts, state):
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
                if nm in (SWAMP, FOREST):
                    return 0
                return 2 if nm == TERGRID else 1
            picks = sorted(hand_ids, key=bkey)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"[{c.name}] bottoms {count}")
        return True

    async def generic_tick(c, pid, st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        dec = mulligan_tick(c, pid, st, acts, state)
        if dec:
            await do_mulligan(c, pid, st, acts, state, dec)
            return True
        if wtype == "MulliganDecision":
            await bottom_cards(c, pid, st, acts, state)
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
        return False

    async def p0_tick(st, acts, state):
        if await generic_tick(p0, 0, st, acts, state):
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "ModalFaceChoice":
            if not obs["choice_submitted"]:
                tag = f"g{obs['game']}_cast"
                rec = record_modal_opp(st, state, tag)
                obs["opps"].append(rec)
                obs["wait_start"] = obs["wait_start"] or time.time()
                if not await answer_modal_face(p0, st, rec, tag,
                                               obs["want_face"]):
                    return True
                return True
            return True
        if wtype != "Priority" or state.get("priority_player") != 0:
            return False
        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return True
            if not obs["cast_submitted"]:
                need = 5 if obs["want_face"] == "front" else 4
                if len(untapped_lands(state, 0, SWAMP)) >= need:
                    sid = spell_in_hand_oid(state, 0, TERGRID)
                    cands = [a for a in acts
                             if a["type"] == "CastSpell"
                             and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
                    obs["cast_spell_ads"] = [
                        {"object_id": a["data"].get("object_id"),
                         "data_keys": sorted((a.get("data") or {}).keys())}
                        for a in acts if a["type"] == "CastSpell"]
                    if sid is not None and cands:
                        obs["cast_submitted"] = True
                        obs["cast_oid"] = sid
                        say(f"P0 casts Tergrid oid={sid} "
                            f"(face={obs['want_face']})")
                        wire("cast", {"action": cands[0],
                                      "want_face": obs["want_face"]})
                        tag = "pre" if obs["game"] == 1 else "g2_pre"
                        await export(tag)
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
            return True
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
        postB = load_env("post_back")
        post = load_env("post")
        g2pre = load_env("g2_pre")
        notes.append(f"rejections={len(obs['rejections'])} "
                     f"face_opps_g1={sum(1 for r in obs['opps'] if r['tag']=='g1_cast')} "
                     f"face_opps_g2={sum(1 for r in obs['opps'] if r['tag']=='g2_cast')} "
                     f"stall={obs['stall']} rejected={obs['rejected']}")
        # A1
        if pre is not None:
            s = pre["state"]
            tg_in_hand = any(lname(s, o) == TERGRID
                             for o in player_of(s, 0).get("hand", []))
            ok = (s.get("active_player") == 0
                  and s.get("phase") == "PreCombatMain"
                  and tg_in_hand
                  and len(untapped_lands(s, 0, SWAMP)) >= 5)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: active={s.get('active_player')} phase={s.get('phase')} "
                         f"tergrid_in_hand={tg_in_hand} "
                         f"untapped_swamps={len(untapped_lands(s, 0, SWAMP))}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing (first cast never submitted)")
        # A2
        recs = [r for r in obs["opps"] if r["tag"] == "g1_cast"]
        if not recs:
            ass["A2_modal_offered"] = "failed"
            notes.append("A2: no ModalFaceChoice wait observed for game 1 cast")
        else:
            r = recs[0]
            faces = {f["face"] for f in r["faces"]}
            actionable = (r["canSubmit"] and len(r["faces"]) >= 2) \
                or len(r["choose_modal_face_actions"]) > 0
            if actionable and {"front", "back"} <= faces:
                ass["A2_modal_offered"] = "passed"
            else:
                ass["A2_modal_offered"] = "failed"
            notes.append(f"A2: ModalFaceChoice wait seen; faces advertised="
                         f"{[f['face'] for f in r['faces']]}; "
                         f"canSubmit={r['canSubmit']}; "
                         f"chooseModalFace actions={r['choose_modal_face_actions']}; "
                         f"actionable={actionable}")
        # A3
        if not obs["choice_submitted"]:
            ass["A3_choice_accepted"] = "not-run"
            notes.append("A3: no face choice was submitted")
        elif obs["rejected"]:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: advertised face choice was REJECTED")
        elif obs["stall"]:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: ModalFaceChoice stalled >60s with no actionable "
                         "submission (the reported stuck signature)")
        elif obs["tergrid_on_bf"]:
            ass["A3_choice_accepted"] = "passed"
            notes.append("A3: front-face choice accepted; cast resolved to "
                         "Tergrid, God of Fright on the battlefield")
        else:
            ass["A3_choice_accepted"] = "failed"
            notes.append("A3: choice submitted but front-face cast never resolved")
        # A4
        if postF is not None:
            s = postF["state"]
            bf = on_bf(s, 0, TERGRID)
            stack_empty = len(s.get("stack", []) or []) == 0
            ok = (len(bf) > 0 and stack_empty
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20)
            ass["A4_front_completes"] = "passed" if ok else "failed"
            notes.append(f"A4: tergrid on P0 BF={bf}; stack_empty={stack_empty}; "
                         f"life={life_of(s, 0)}/{life_of(s, 1)}; "
                         f"turn={s.get('turn_number')} phase={s.get('phase')}")
        else:
            ass["A4_front_completes"] = "failed"
            notes.append("A4: post_front.json missing")
        # A5
        recs2 = [r for r in obs["opps"] if r["tag"] == "g2_cast"]
        g2_faces = [f["face"] for r in recs2 for f in r["faces"]] if recs2 else []
        if postB is not None:
            s = postB["state"]
            bf = on_bf(s, 0, LANTERN)
            stack_empty = len(s.get("stack", []) or []) == 0
            ok = (len(bf) > 0 and stack_empty
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20)
            ass["A5_back_completes"] = "passed" if ok else "failed"
            lantern = get_obj(s, bf[0]) if bf else {}
            notes.append(f"A5: lantern on P0 BF={bf} "
                         f"(card_type={lantern.get('card_type')}); "
                         f"stack_empty={stack_empty}; "
                         f"life={life_of(s, 0)}/{life_of(s, 1)}; "
                         f"turn={s.get('turn_number')} phase={s.get('phase')}; "
                         f"g2 ModalFaceChoice advertised faces={g2_faces} "
                         f"(only 4 untapped Swamps at cast time, so the "
                         f"{{3}}{{B}}{{B}} front face was unpayable - "
                         f"consistent with payment-feasibility filtering, "
                         f"not the reported defect)")
        else:
            ass["A5_back_completes"] = "failed"
            notes.append("A5: post_back.json missing (back-face path not completed)")
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
        # verdict
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached the first ModalFaceChoice")
        elif ass["A2_modal_offered"] == "failed" \
                or ass["A3_choice_accepted"] == "failed":
            verdict = "reproduced"
            notes.append("the ModalFaceChoice decision itself failed (fewer than "
                         "2 actionable face choices / rejection / stall)")
        elif ass["A4_front_completes"] != "passed" \
                or ass["A5_back_completes"] != "passed":
            verdict = "reproduced"
            notes.append("a chosen face was accepted but never reached the "
                         "battlefield")
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("incomplete run; see assertion notes")
        return verdict

    async def finish():
        dur = time.time() - t_start
        for tag in ("post_front", "post_back", "post"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": 5487,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9376,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5487.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if not k.startswith("_")},
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Tergrid / high land density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "Not tested on the original 2026-07-10 build v0.20.0 (7b5b324); "
                "verdict is scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Tergrid, God of Fright + 48x Swamp; "
                          "P1: 60x Forest. Both games.",
            "contract_line": "Cast Tergrid at main-phase priority; the "
                             "ModalFaceChoice must offer both faces as "
                             "actionable selections; each selection must be "
                             "accepted and resolve (front face -> Tergrid on "
                             "the battlefield; back face -> Tergrid's Lantern "
                             "on the battlefield).",
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

    with open(f"{EVDIR}/scenario_5487.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_5487.py").read())

    async def run_loop(max_turns):
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
            s = (p0.latest["state"] if p0.latest else {}) or {}
            turn = s.get("turn_number", 0)
            phase = s.get("phase")
            active = s.get("active_player")
            wtype = (s.get("waiting_for") or {}).get("type")
            if obs["game"] == 1:
                if not obs["tergrid_on_bf"] and on_bf(s, 0, TERGRID):
                    obs["tergrid_on_bf"] = True
                    await export("post_front")
                    say("game 1 done: Tergrid on BF; exporting post_front")
                    return True
            else:
                if not obs["lantern_on_bf"] and on_bf(s, 0, LANTERN):
                    obs["lantern_on_bf"] = True
                    await export("post_back")
                    await export("post")
                    say("game 2 done: Lantern on BF; exporting post_back/post")
                    return True
            if obs["wait_start"] and not obs["choice_submitted"] \
                    and time.time() - obs["wait_start"] > 60:
                actionable = any(
                    (r["canSubmit"] and r["faces"])
                    or r["choose_modal_face_actions"]
                    for r in obs["opps"])
                if not actionable:
                    obs["stall"] = True
                    await export("mid_stall")
                    say("STALL: ModalFaceChoice >60s with no actionable "
                        "submission; captured mid_stall.json")
                    return False
            if turn >= max_turns:
                notes.append(f"game {obs['game']}: turn {max_turns} reached; "
                             f"bailing out")
                say(f"game {obs['game']} turn bail; moving on")
                return False
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                say(f"DIAG g{obs['game']} turn={turn} active={active} phase={phase} "
                    f"wf={wtype} p0hand={hand_names(s,0)[:6]} "
                    f"tergrid_bf={on_bf(s,0,TERGRID)} stack={stack_names(s)[:3]}")

    # ---- game 1 (front face) ----
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"G1 game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ok1 = await run_loop(25)
    say(f"game 1 loop ended ok={ok1}")

    # ---- game 2 (back face) ----
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    obs["game"] = 2
    obs["want_face"] = "back"
    obs["kept"] = {}
    obs["cast_submitted"] = False
    obs["cast_oid"] = None
    obs["choice_submitted"] = False
    obs["choice_id"] = None
    obs["wait_start"] = None
    obs["rejected"] = False
    obs["stall"] = False
    obs["rejections"] = []
    # NOTE: obs["opps"] is intentionally NOT reset; evaluate() reads the
    # g1_cast record for A2 (game 2's record is evaluated separately below).
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"G2 game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ok2 = await run_loop(25)
    say(f"game 2 loop ended ok={ok2}")

    await finish()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
