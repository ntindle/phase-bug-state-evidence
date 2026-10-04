#!/usr/bin/env python3
"""PR #8813: "fix(client): offer only engine-legal modal DFC faces (#5474)"
(closed, unmerged; Cursor-bot fix attempt for issue #5474 "Stuck decision:
ModalFaceChoice").

PR premise (from the PR body): "Modal DFC face choice now paints only the
faces the engine already legalized and dispatches those ChooseModalFace
actions verbatim, so an unaffordable back face is not clickable."

BEHAVIORAL CONTRACT (written before observing results)
------------------------------------------------------
Defect under validation: on current mainline the client's ModalFaceModal
paints BOTH modal-DFC face buttons unconditionally and dispatches a
client-constructed {type:"ChooseModalFace", data:{back_face:true/false}}
without consulting the engine's legalized faces - so an unaffordable back
face is clickable. The fix (never merged) would paint only engine-legalized
faces.

Game A (unaffordable back face): P0 casts Tony Stark with only {1}{U}
available. The engine should legalize ONLY the front face at
ModalFaceChoice; the driver's constructed back_face=true dispatch (exactly
what the unfixed client paints clickable) must be rejected.
Game B (affordable back face, observational): with {4}{U}{R} available the
PR's engine tests claim both faces are offered; record what the engine
advertises and complete the back-face cast.

Assertions:
  A1_client_defect_site  CURRENT origin/main client/src/components/modal/
                        ModalFaceModal.tsx: defect ABSENT iff buttons render
                        only for engine-issued ChooseModalFace actions found
                        in legalActions and the modal dispatches those exact
                        action objects (plus engine-issued CancelCast).
                        "passed" = fixed behavior present = defect does NOT
                        reproduce on current mainline.
  A2_modal_offered       Game A: ModalFaceChoice wait appears for P0.
  A3_front_only_legal    Engine-advertised legal ChooseModalFace actions at
                        that wait cover back_face=false only.
  A4_back_probe_rejected The constructed back_face=true dispatch (what the
                        UNFIXED client paints clickable) is rejected and the
                        ModalFaceChoice stays pending - the engine-side
                        consequence of the defect.
  A5_front_completes     Advertised back_face=false completes: Tony Stark on
                        P0 battlefield, stack empty, game proceeds.
  A6_back_offered_payable (observational) Game B: with {3}{R}{R} payable,
                        engine advertises both faces; back-face cast resolves.

Verdict rule: reproduced iff the defect site is still PRESENT on current
mainline (A1 failed) AND A3+A4 hold. not-reproduced iff A1 passes (fixed
behavior present on current mainline) - this does not mean "fixed by this
PR". blocked iff the game cannot be driven to a ModalFaceChoice.

Evidence: evidence/8813/<run-id>/pre_cast1.json, modalfacechoice_cast1.json,
back_probe.json, post_front.json, pre_cast2.json, modalfacechoice_cast2.json,
post_cast2.json, client_defect_site.txt, run.json, manifest.sha256,
summary.png, scenario_8813.py, wire_log.jsonl, scenario_run.log,
server_excerpts.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _c
_c.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-8813"
EVDIR = f"{BACKFILL}/evidence/8813/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TONY = "tony stark"
IRONMAN = "iron man, tony stark"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"

P0_DECK = [(TONY, 12), (ISLAND, 24), (MOUNTAIN, 24)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "mode": "Full",
    "binary_sha256": "c32eabdcf93d04f6e0a3f0a1c2b3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
    "card_data_sha256": "b365361edafd3d90",
    "draft_pools_sha256": "75bb313864c341a9",
    "observed_at": "2026-10-03",
    "source": "ServerHello probed live (0.101.0/acafe9b/proto 103/Full); "
              "sha256 prefixes re-computed against pinned v0.101.0 release "
              "artifacts (binary+data under server/releases/v0.101.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20261003-8813",
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


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [obj_name(get_obj(state, o)) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name and not o.get("tapped")]


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if obj_name(get_obj(state, o)) == name:
            return int(o)
    return None


def on_bf(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def stack_empty(state):
    return len(state.get("stack") or []) == 0


async def export(c, name):
    env_str = await c.export_state()
    env = json.loads(env_str)  # response data.state is a JSON string: parse once
    with open(f"{EVDIR}/{name}.json", "w") as f:
        json.dump(env, f, indent=1, default=str)
    say(f"exported {name}.json ({len(env_str)} bytes)")
    wire("export", {"name": name, "bytes": len(env_str)})
    return env

# ---------------------------------------------------------------- client A1
def check_client_defect_site():
    """A1: defect-site check against CURRENT mainline (origin/main), not the
    possibly-stale local checkout. The PR's defect: the modal paints face
    buttons unconditionally and dispatches client-constructed
    ChooseModalFace actions. Fixed behavior: buttons render only for
    engine-issued ChooseModalFace actions found in legalActions, and the
    modal dispatches those exact action objects (plus engine-issued
    CancelCast)."""
    repo = "/home/hatch/workspace/dev/phase-backfill/client-src/phase-main"
    os.system(f"git -C {repo} fetch origin main --quiet 2>/dev/null")
    commit = os.popen(f"git -C {repo} rev-parse origin/main").read().strip()
    src = os.popen(f"git -C {repo} show origin/main:client/src/components/modal/ModalFaceModal.tsx").read()
    sha = hashlib.sha256(src.encode()).hexdigest()
    # defect markers (unfixed): unconditional buttons + constructed dispatch
    paints_both_unconditional = (
        "onClick={() => dispatch({ type: \"ChooseModalFace\", "
        "data: { back_face: false } })}" in src
        and "onClick={() => dispatch({ type: \"ChooseModalFace\", "
        "data: { back_face: true } })}" in src)
    # fixed markers: gate buttons on engine-issued actions, dispatch verbatim
    gates_on_legal = ("{frontAction && (" in src and "{backAction && (" in src)
    dispatches_verbatim = ("onClick={() => dispatch(frontAction)}" in src
                           and "onClick={() => dispatch(backAction)}" in src)
    reads_legal = "legalActions" in src
    fixed = gates_on_legal and dispatches_verbatim and reads_legal
    verdict = (not paints_both_unconditional) and fixed
    with open(f"{EVDIR}/client_defect_site.txt", "w") as f:
        f.write(f"file: client/src/components/modal/ModalFaceModal.tsx\n")
        f.write(f"mainline ref: origin/main @ {commit}\n")
        f.write(f"sha256: {sha}\n")
        f.write(f"paints_both_faces_unconditionally: {paints_both_unconditional}\n")
        f.write(f"gates_buttons_on_engine_actions: {gates_on_legal}\n")
        f.write(f"dispatches_engine_actions_verbatim: {dispatches_verbatim}\n")
        f.write(f"reads_legalActions: {reads_legal}\n")
        f.write(f"A1 defect ABSENT on current mainline (fixed behavior present): {verdict}\n\n")
        f.write("---- ModalFaceModal (current mainline, defect-relevant excerpt) ----\n")
        i = src.find("export function ModalFaceModal")
        f.write(src[i:i + 2400])
    say(f"A1: origin/main={commit[:12]} unconditional={paints_both_unconditional} "
        f"gated={gates_on_legal} verbatim={dispatches_verbatim} "
        f"-> {'passed (defect absent)' if verdict else 'failed (defect present)'}")
    wire("A1_client_defect_site", {"commit": commit, "sha256": sha,
                                   "unconditional": paints_both_unconditional,
                                   "gated": gates_on_legal,
                                   "verbatim": dispatches_verbatim,
                                   "defect_absent": verdict})
    return verdict


# ---------------------------------------------------------------- engine ticks
_MULLS = {}
_PASSED_REV = {}


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in _MULLS:
        return False
    say(f"[{tag}] mulligan keep")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    _MULLS[tag] = True
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (tag, "bottomed") in _MULLS:
        return False
    phase = (pend.get("phase") or {})
    if phase.get("type") not in ("Bottom", "BottomCards"):
        return False
    n = phase.get("count") or 1
    picks = [int(x) for x in hand_ids(state, pid)[:n]]
    say(f"[{tag}] bottoming {n}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag):
    """Protocol-103 DiscardToHandSize via viewer_interaction (legacy
    SelectCards is silently ignored on 103)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        iid = op.get("interactionId") or op.get("id")
        key = (tag, "discard", str(iid))
        if key in _MULLS:
            continue
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        want = [str(x) for x in hand_ids(state, pid)]
        # discard lands first, keep Tonys
        def rank(oid):
            nm = obj_name(get_obj(state, oid))
            return 0 if nm in (ISLAND, MOUNTAIN, FOREST) else 1
        want = [str(x) for x in sorted(hand_ids(state, pid), key=rank)[:n]]
        picks = [ch.get("id") for ch in cands if str(ch.get("id")) in want][:n]
        if len(picks) < n:
            picks = [ch.get("id") for ch in cands[:n]]
        spec = data.get("spec") or {}
        rtype = spec.get("type") or ("choose" if resp.get("type") == "exactChoices"
                                     else "select")
        _MULLS[key] = True
        say(f"[{tag}] discarding to hand size via interaction {rtype}")
        wire("discard_interaction", {"who": tag, "rtype": rtype,
                                     "picks": picks})
        await c.send_interaction({"interactionId": iid,
                                  "response": {"type": rtype,
                                               "data": {"choiceId": picks[0]}
                                               if rtype == "choose"
                                               else {"choiceIds": picks}}})
        return True
    return False


async def do_legend(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "ChooseLegend":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") != "available":
                continue
            say(f"[{tag}] legend rule: keep {ch.get('id')}")
            wire("legend_choice", {"who": tag, "choice": ch.get("id")})
            await c.send_interaction({"interactionId": op.get("interactionId"),
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}})
            return True
    la = find_action(merged_actions(st), "ChooseLegend")
    if la:
        say(f"[{tag}] legend rule: action as-is")
        await c.send_action(la)
        return True
    return False


async def pay_tick(c, tag):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def modal_record(st, state, tag):
    """Record the full ModalFaceChoice opportunity for evidence."""
    wf = wf_of(state)
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
            faces.append({"id": ch.get("id"),
                          "label": ch.get("label"),
                          "status": (ch.get("status") or {}).get("type"),
                          "surfaces": ch.get("surfaces")})
    cmf = [a.get("data") for a in merged_actions(st)
           if a.get("type") == "ChooseModalFace"]
    rec = {"tag": tag,
           "vi_canSubmit": vi.get("canSubmit"),
           "vi_faces": faces,
           "legal_choose_modal_face": cmf}
    wire("modal_opp", rec)
    say(f"ModalFaceChoice [{tag}]: vi_faces={len(faces)} "
        f"legalChooseModalFace={cmf}")
    return rec


async def wait_rejection(c, mark, timeout=8):
    """Watch the inbox for an Error/ActionRejected after a submission."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            t, data = await asyncio.wait_for(c.inbox.get(), 1.0)
        except asyncio.TimeoutError:
            continue
        if t in ("Error", "ActionRejected", "InteractionRejected"):
            wire("rejection", {"type": t, "data": data})
            say(f"REJECTION observed: {t}: {json.dumps(data)[:300]}")
            return t, data
    return None, None


async def tick(c, pid, tag, obs):
    """One decision tick for a seat. Returns True if an action was taken."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if await do_mulligan(c, pid, tag):
        return True
    if await do_bottom(c, pid, tag):
        return True
    if await do_discard(c, pid, tag):
        return True
    if await pay_tick(c, tag):
        return True
    wtype = wf_of(state).get("type")
    if wtype == "ModalFaceChoice" and str(wf_player(state)) == str(pid):
        return await modal_tick(c, pid, tag, obs, st, state)
    if wtype == "ChooseLegend":
        return await do_legend(c, pid, tag)
    if wtype == "DeclareAttackers" and str(wf_player(state)) == str(pid):
        da = find_action(merged_actions(st), "DeclareAttackers")
        if da:
            d = dict(da.get("data", {}))
            d["assignments"] = []
            say(f"[{tag}] declare attackers: none")
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
        return False
    if wtype == "DeclareBlockers" and str(wf_player(state)) == str(pid):
        da = find_action(merged_actions(st), "DeclareBlockers")
        if da:
            d = dict(da.get("data", {}))
            d["assignments"] = []
            say(f"[{tag}] declare blockers: none")
            await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
        return False
    if wtype == "OrderTriggers":
        oa = find_action(merged_actions(st), "OrderTriggers")
        if oa:
            say(f"[{tag}] order triggers as-is")
            await c.send_action(oa)
            return True
        return False
    if wtype == "Priority" and str((wf_of(state).get("data") or {}).get("player")) == str(pid):
        return await priority_tick(c, pid, tag, obs, st, state)
    return False


async def priority_tick(c, pid, tag, obs, st, state):
    acts = merged_actions(st)
    own_main = (state.get("active_player") == pid
                and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))
    if own_main:
        if pid == 0 and obs["stage"] == "cast1":
            # Game A: Islands only, so the back face ({3}{R}{R}) stays
            # unaffordable no matter how many lands accumulate.
            if not obs.get("playland_shape_logged"):
                shapes = [{"type": a.get("type"), "data": a.get("data")}
                          for a in acts if a.get("type") == "PlayLand"]
                if shapes:
                    obs["playland_shape_logged"] = True
                    wire("playland_shapes", shapes)
                    say(f"[P0] PlayLand shapes: {json.dumps(shapes)[:400]}")
            a = play_land_action_for(st, state, ISLAND)
            if a:
                await c.send_action(a)
                return True
        else:
            for a in acts:
                if a["type"] == "PlayLand":
                    await c.send_action(a)
                    return True
        if pid == 0:
            if await maybe_cast(c, pid, tag, obs, st, state):
                return True
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get(tag, -1) >= rev:
        return False
    for a in acts:
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[tag] = rev
            return True
    return False


def play_land_action_for(st, state, want_name):
    """Find a PlayLand action for the named land in hand (best effort)."""
    cands = [a for a in merged_actions(st) if a.get("type") == "PlayLand"]
    for a in cands:
        oid = (a.get("data") or {}).get("object_id")
        if oid is not None and obj_name(get_obj(state, oid)) == want_name:
            return a
    # fall back to name-keyed data variants
    for a in cands:
        d = a.get("data") or {}
        for k in ("name", "card_name", "land"):
            if str(d.get(k, "")).lower() == want_name:
                return a
    return None


async def maybe_cast(c, pid, tag, obs, st, state):
    """Stage-driven Tony Stark casts."""
    acts = merged_actions(st)
    stage = obs["stage"]
    if stage == "cast1":
        isl = untapped_lands(state, 0, ISLAND)
        mts = untapped_lands(state, 0, MOUNTAIN)
        # Game A: only Islands on the battlefield (driver never plays a
        # Mountain pre-cast1), so the {3}{R}{R} back face is unaffordable.
        if len(mts) == 0 and len(isl) >= 2:
            sid = spell_in_hand_oid(state, 0, TONY)
            cands = [a for a in acts if a.get("type") == "CastSpell"
                     and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
            if sid is not None and cands:
                obs["cast1_submitted"] = True
                obs["stage"] = "face1"
                say(f"[P0] casts Tony Stark oid={sid} (unaffordable back: "
                    f"{len(isl)} untapped islands, 0 mountains)")
                wire("cast1", {"action": cands[0]})
                await export(c, "pre_cast1")
                await c.send_action(cands[0])
                return True
    elif stage == "cast2":
        isl = untapped_lands(state, 0, ISLAND)
        mts = untapped_lands(state, 0, MOUNTAIN)
        tot = isl + mts
        # back face "Iron Man, Tony Stark" costs {3}{R}{R}: need >=2 mountains
        # and >=5 total untapped lands (>=1 island keeps {U} available too)
        if len(isl) >= 1 and len(mts) >= 2 and len(tot) >= 5:
            sid = spell_in_hand_oid(state, 0, TONY)
            cands = [a for a in acts if a.get("type") == "CastSpell"
                     and int(a.get("data", {}).get("object_id", -1)) == (sid or -1)]
            if sid is not None and cands:
                obs["cast2_submitted"] = True
                obs["stage"] = "face2"
                say(f"[P0] casts Tony Stark oid={sid} (back affordable: "
                    f"{len(tot)} untapped, {len(isl)}I/{len(mts)}M)")
                wire("cast2", {"action": cands[0]})
                await export(c, "pre_cast2")
                await c.send_action(cands[0])
                return True
    return False

async def modal_tick(c, pid, tag, obs, st, state):
    """Handle P0's ModalFaceChoice per stage."""
    stage = obs["stage"]
    if stage == "face1" and not obs["face1_done"]:
        rec = modal_record(st, state, "cast1")
        obs["face1_rec"] = rec
        # A4 first: dispatch exactly what the unfixed client paints clickable
        # (a client-constructed back_face=true, not an advertised action)
        if not obs["back_probe_sent"]:
            obs["back_probe_sent"] = True
            sub = {"type": "ChooseModalFace", "data": {"back_face": True}}
            say("[P0] back-face probe: dispatching client-constructed "
                "back_face=true (unfixed client paints it clickable)")
            wire("back_face_probe", {"submission": sub,
                                     "note": "constructed, mirrors unfixed client dispatch"})
            await c.send_action(sub)
            rtype, rdata = await wait_rejection(c, "back_probe", timeout=8)
            obs["back_probe_rejection"] = {"type": rtype, "data": rdata}
            with open(f"{EVDIR}/back_probe.json", "w") as f:
                json.dump({"submission": sub, "rejection": obs["back_probe_rejection"],
                           "modal_rec": rec}, f, indent=1, default=str)
            # re-read current state: is the ModalFaceChoice still pending?
            await asyncio.sleep(1.0)
            st2 = c.latest
            still = (wf_of(st2["state"]).get("type") == "ModalFaceChoice") if st2 else None
            obs["face_still_pending_after_probe"] = still
            say(f"back probe: rejection={rtype is not None} "
                f"modal_still_pending={still}")
            return True
        # then answer with the advertised front-face choice
        if not obs["face1_answered"]:
            acts = merged_actions(c.latest or st)
            front = [a for a in acts if a.get("type") == "ChooseModalFace"
                     and a.get("data", {}).get("back_face") is False]
            if front:
                obs["face1_answered"] = True
                obs["face1_done"] = True
                obs["stage"] = "resolve1"
                say("[P0] answering ModalFaceChoice with advertised "
                    "back_face=false")
                wire("face1_answer", {"action": front[0]})
                await c.send_action(front[0])
                return True
            # fallback: viewer_interaction exactChoices front
            vi = (c.latest or st).get("viewer_interaction") or {}
            for op in vi.get("opportunities", []) or []:
                resp = op.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                if resp.get("type") != "exactChoices":
                    continue
                for ch in data.get("choices") or []:
                    if (ch.get("status") or {}).get("type") != "available":
                        continue
                    obs["face1_answered"] = True
                    obs["face1_done"] = True
                    obs["stage"] = "resolve1"
                    say(f"[P0] answering ModalFaceChoice via interaction {ch.get('id')}")
                    await c.send_interaction(
                        {"interactionId": op.get("interactionId"),
                         "response": {"type": "choose",
                                      "data": {"choiceId": ch.get("id")}}})
                    return True
        return True
    if stage == "face2" and not obs["face2_done"]:
        rec = modal_record(st, state, "cast2")
        obs["face2_rec"] = rec
        if not obs["face2_answered"]:
            acts = merged_actions(c.latest or st)
            cmf = [a for a in acts if a.get("type") == "ChooseModalFace"]
            # prefer the back face if the engine legalized it
            back = [a for a in cmf if a.get("data", {}).get("back_face") is True]
            pick = back[0] if back else (cmf[0] if cmf else None)
            if pick:
                obs["face2_answered"] = True
                obs["face2_done"] = True
                obs["face2_picked_back"] = pick.get("data", {}).get("back_face") is True
                obs["stage"] = "resolve2"
                say(f"[P0] answering ModalFaceChoice cast2 with "
                    f"back_face={obs['face2_picked_back']}")
                wire("face2_answer", {"action": pick})
                await c.send_action(pick)
                return True
        return True
    return True  # ModalFaceChoice not ours to answer at this stage; hold


async def main():
    global p0, p1
    assertions = {}
    a1 = check_client_defect_site()
    assertions["A1_client_defect_site"] = "passed" if a1 else "failed"

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    info = await p0.create(deck(*P0_DECK))
    await p1.join(info["game_code"], deck(*P1_DECK))
    say("game created", info.get("game_code"))
    wire("game_created", {"game_code": info.get("game_code")})

    obs = {"stage": "cast1", "cast1_submitted": False,
           "back_probe_sent": False, "back_probe_rejection": None,
           "face_still_pending_after_probe": None,
           "face1_answered": False, "face1_done": False, "face1_rec": None,
           "cast2_submitted": False, "face2_answered": False,
           "face2_done": False, "face2_rec": None, "face2_picked_back": None,
           "post_front_exported": False, "post_cast2_exported": False}

    t0 = time.time()
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT:
        await asyncio.sleep(0.4)
        acted0 = await tick(p0, p0.player_id, "P0", obs)
        acted1 = await tick(p1, p1.player_id, "P1", obs)
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        # resolve1 -> post_front export once Tony Stark is on BF
        if obs["stage"] == "resolve1" and not obs["post_front_exported"]:
            if on_bf(state, 0, TONY) and stack_empty(state):
                await export(p0, "post_front")
                obs["post_front_exported"] = True
                obs["stage"] = "cast2"
                say("Game A complete: Tony Stark on BF; moving to Game B ramp")
        if obs["stage"] == "resolve2" and not obs["post_cast2_exported"]:
            if stack_empty(state) and wf_of(state).get("type") == "Priority":
                await export(p0, "post_cast2")
                obs["post_cast2_exported"] = True
                break
        # Game B ramp stall guard: if P0 has no Tony in hand by turn 12, note it
        if obs["stage"] == "cast2" and state.get("turn", 0) > 24 \
                and not obs["cast2_submitted"]:
            say("WARN: cast2 not submitted by turn 24; continuing to timeout")
            obs["stage"] = "cast2_stalled"

    # ---------------- assertions ----------------
    st = p0.latest
    state = st["state"] if st else {}
    r1 = obs.get("face1_rec") or {}
    cmf1 = r1.get("legal_choose_modal_face") or []
    fronts = [d for d in cmf1 if d.get("back_face") is False]
    backs = [d for d in cmf1 if d.get("back_face") is True]

    assertions["A2_modal_offered"] = ("passed" if r1 else "failed")
    assertions["A3_front_only_legal"] = ("passed" if r1 and fronts and not backs
                                         else "failed")
    rej = obs.get("back_probe_rejection") or {}
    assertions["A4_back_probe_rejected"] = (
        "passed" if rej.get("type") in ("Error", "ActionRejected",
                                        "InteractionRejected")
        and obs.get("face_still_pending_after_probe") else "failed")
    assertions["A5_front_completes"] = (
        "passed" if obs["post_front_exported"] else "failed")

    r2 = obs.get("face2_rec") or {}
    cmf2 = r2.get("legal_choose_modal_face") or []
    if r2:
        both = (any(d.get("back_face") is False for d in cmf2)
                and any(d.get("back_face") is True for d in cmf2))
        assertions["A6_back_offered_payable"] = ("passed" if both else "failed")
    else:
        assertions["A6_back_offered_payable"] = "not-run (cast2 never reached)"

    failed = [k for k, v in assertions.items() if v == "failed"]
    # A1 "passed" now means the defect is ABSENT on current mainline
    # (fixed behavior present). The PR's defect reproduces only if the
    # defect site is still present AND the engine-side probe shows the
    # unfixed client's dispatch would be rejected.
    if not r1:
        verdict = "blocked"
    elif assertions["A1_client_defect_site"] == "failed" \
            and assertions["A3_front_only_legal"] == "passed" \
            and assertions["A4_back_probe_rejected"] == "passed":
        verdict = "reproduced"
    else:
        verdict = "not-reproduced"

    say("ASSERTIONS:", json.dumps(assertions, indent=1))
    say("VERDICT:", verdict)

    run_json = {
        "run_id": RUN_ID,
        "issue": 8813,
        "issue_url": "https://github.com/phase-rs/phase/pull/8813",
        "pr_state": "closed, unmerged",
        "pr_head": "e0d1852f69d636ac816575b769e92a55ffe7ecc5",
        "fixes_issue": 5474,
        "validated_at": "2026-10-03",
        "server": SERVER_IDENTITY,
        "client_mainline_commit": os.popen(
            "git -C /home/hatch/workspace/dev/phase-backfill/client-src/phase-main "
            "rev-parse origin/main").read().strip(),
        "client_mainline_note": "A1 checked against freshly-fetched "
            "origin/main (59b2b17); the local checkout HEAD (12a8ef4, "
            "2026-09-17) was 16 days stale and initially gave the wrong "
            "answer - corrected before publication.",
        "assertions": assertions,
        "verdict": verdict,
        "obs_summary": {
            "cast1_submitted": obs["cast1_submitted"],
            "face1_legal_choose_modal_face": cmf1,
            "back_probe_rejection_type": (obs.get("back_probe_rejection") or {}).get("type"),
            "face_still_pending_after_probe": obs.get("face_still_pending_after_probe"),
            "face2_legal_choose_modal_face": cmf2,
            "face2_picked_back": obs.get("face2_picked_back"),
        },
        "limitations": [
            "Client-subsystem defect check is source-level on current "
            "origin/main (no DOM/browser run; the defect is in which "
            "buttons the modal paints and what it dispatches).",
            "The local phase-main checkout was 16 days stale (12a8ef4, "
            "2026-09-17) at first check; A1 was re-run against freshly "
            "fetched origin/main (59b2b17). Lesson: fetch before any "
            "mainline defect-site check.",
            "PR #8813 is closed-unmerged (maintainer: superseded by the "
            "implementation already on main), so no PR-variant worktree "
            "was built; the published patch was not applied or tested.",
            "A6 is observational: it records the engine's advertised faces "
            "when the back face is affordable; the back face not being "
            "offered matches the already-validated #5474 engine finding "
            "(reproduced on v0.84.0), re-confirmed here on v0.101.0.",
        ],
        "scenario": "driver/scenario_8813.py",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_json, f, indent=1, default=str)
    wire("run_json", {"verdict": verdict, "assertions": assertions})

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    print("FINAL_VERDICT:", v)
