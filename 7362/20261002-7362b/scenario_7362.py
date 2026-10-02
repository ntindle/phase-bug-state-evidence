#!/usr/bin/env python3
"""Issue #7362: Surveil -- "Surveil is back to being a hidden card."

Reported (Discord, solo vs AI): the surveil prompt renders the surveilled
card(s) as hidden to the surveilling player. Root cause found by matthewevans
(2026-08-29): engine `filter_state_for_viewer` redacts every library object
and un-redacts through an enumerated allowlist; `WaitingFor::SurveilChoice`
was never in it (CR 701.25a "look at the top N cards of your library" not
covered), and `SurveilChoice.cards` was not redacted for non-prompt viewers.
Fix enqueued as PR #8187 (merged 2026-08-29). Issue carries
status:fixed-unreleased.

Behavioral contract (native engine, two human-client seats, v0.99.0/proto 98):
  SETUP  - P0: 12x Otherworldly Gaze + 48x Island; P1: 60x Island. Both keep.
  CAST   - P0 plays an Island per own main phase, casts Otherworldly Gaze
           ({U}: surveil 3) at the first opportunity.
  CAPTURE- when SurveilChoice pends for P0, BEFORE answering: wait until P1's
           client also holds a snapshot at that revision; dump P0's
           waiting_for payload; authoritative-export the game (ground truth);
           for each surveilled card id record how the object appears in
           (a) the authoritative export, (b) P0's viewer-filtered view,
           (c) P1's viewer-filtered view, and (d) each view's waiting_for
           payload (cards id array redaction for non-prompt viewers).
  ANSWER - answer the surveil (select all candidates = keep on top, the
           verified select semantics), decline Gaze's optional return.
  DONE   - Gaze resolves; stack empty; export post.

  A1 setup_ok            SurveilChoice observed for P0 with >=1 candidate ids.
  A2 surveiller_sees     every surveilled card shows its TRUE name in P0's
                         viewer-filtered view (face-up to the surveilling
                         player, per the acceptance criteria).
  A3 opponent_blind     no surveilled card shows its true name in P1's
                         viewer-filtered view, and P1's waiting_for payload
                         (if any) does not carry the unredacted cards ids.
  A4 choice_completes    surveil answered; Gaze resolved to graveyard/hand;
                         SurveilChoice no longer pending.
  A5 cleanup             post state: stack empty, game advancing.

Verdict = blocked iff A1 fails.
Verdict = reproduced iff A1 passes and (A2 fails or A3 fails).
Verdict = not-reproduced iff A1..A5 all pass. (Not a fix claim: the report
predates the pinned release; fixed-unreleased is the issue's own status.)
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
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261002-7362")
EVID_ISSUE = "7362"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

SERVER_IDENTITY = {
    "version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"

assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
shutil.copy(__file__, f"{EVDIR}/scenario_7362.py")

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

START = time.time()
DEADLINE = 600  # hard stop: 10 minutes


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


GAZE = "Otherworldly Gaze"
ISLAND = "Island"
P0_DECK = deck((GAZE, 12), (ISLAND, 48))
P1_DECK = deck((ISLAND, 60))

ST = {"captured": False, "answered": False, "surveil_rev": None,
      "capture": None, "stop": False, "gaze_resolved": False}
WF_SEEN = []


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_type(state, pid, key):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == key.lower()]


def stack(state):
    return state.get("stack", []) or []


def life(state, pid):
    players = state.get("players", []) or []
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid)) or {}
        return pp.get("life")
    for p in players:
        if isinstance(p, dict) and p.get("id") == pid:
            return p.get("life")
    return None


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def vi_opps(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def cur(c):
    st = c.latest
    if not st:
        return None, [], {}
    state = st.get("state") or {}
    acts = st.get("legal_actions", []) or []
    wf = state.get("waiting_for") or {}
    return state, acts, wf


def surveil_candidate_ids(c):
    """Card object ids offered by the SurveilChoice prompt (P0's view)."""
    ids = []
    for opp in vi_opps(c):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        ids = [str(ch.get("id")) for ch in cands if ch.get("id") is not None]
        if ids:
            break
    return ids


def name_of(obj):
    if not isinstance(obj, dict):
        return None
    return obj.get("base_name") or obj.get("name")


async def capture_surveil(c0, c1):
    """Capture the visibility evidence while SurveilChoice pends for P0.
    Must be called exactly once, before answering."""
    st0 = c0.latest
    state0 = st0.get("state") or {}
    rev = st0.get("state_revision")
    say(f"[capture] SurveilChoice pending at revision {rev}; "
        f"waiting for P1 view to catch up")
    # wait until P1's client holds a snapshot at >= the surveil revision
    t0 = time.time()
    while time.time() - t0 < 30:
        st1 = c1.latest
        if st1 and (st1.get("state_revision") or 0) >= (rev or 0):
            break
        await asyncio.sleep(0.25)
    st1 = c1.latest
    state1 = (st1.get("state") or {}) if st1 else {}
    say(f"[capture] P1 view revision now {st1.get('state_revision') if st1 else None}")

    wf0 = state0.get("waiting_for") or {}
    wf1 = state1.get("waiting_for") or {}
    # Real card object ids come from the waiting_for payload (P0's view is
    # unredacted post-fix; P1's is redacted). Interaction candidate ids
    # (<game>.<rev>.s<N>) are synthetic and do NOT key the objects map.
    d0 = wf0.get("data") or {}
    ids = [str(x) for x in (d0.get("cards") or []) if x]
    if not ids:
        ids = surveil_candidate_ids(c0)
    say(f"[capture] surveilled ids: {ids}")

    # authoritative ground truth
    raw = await c0.export_state()
    true_state = json.loads(raw)["state"]
    with open(f"{EVDIR}/pre_surveil.json", "w") as f:
        f.write(raw)
    say("[capture] exported pre_surveil.json (authoritative)")

    per_card = []
    for oid in ids:
        # objects maps may key by str or int; try both
        def get_obj(m, k):
            if not isinstance(m, dict):
                return None
            return m.get(k) or m.get(int(k)) if str(k).lstrip("-").isdigit() else m.get(k)
        t_obj = get_obj(true_state.get("objects", {}), oid)
        o0 = get_obj(objs(state0), oid)
        o1 = get_obj(objs(state1), oid)
        true_name = name_of(t_obj)
        rec = {
            "object_id": oid,
            "true": {"name": true_name,
                     "zone": (t_obj or {}).get("zone"),
                     "controller": (t_obj or {}).get("controller")},
            "p0_view": {"present": o0 is not None,
                        "name": name_of(o0),
                        "zone": (o0 or {}).get("zone"),
                        "raw_keys": sorted((o0 or {}).keys())[:24]},
            "p1_view": {"present": o1 is not None,
                        "name": name_of(o1),
                        "zone": (o1 or {}).get("zone"),
                        "raw_keys": sorted((o1 or {}).keys())[:24]},
        }
        # keep one full raw object dump for the first card in each view
        if oid == ids[0]:
            rec["p0_raw_object"] = o0
            rec["p1_raw_object"] = o1
            rec["true_raw_object"] = {k: t_obj.get(k) for k in
                                      list((t_obj or {}).keys())[:40]} if t_obj else None
        per_card.append(rec)

    capture = {
        "revision": rev,
        "p1_view_revision": (st1 or {}).get("state_revision"),
        "waiting_for_p0": {"type": wf0.get("type"), "data": wf0.get("data")},
        "waiting_for_p1": {"type": wf1.get("type"), "data": wf1.get("data")},
        "surveilled_ids": ids,
        "per_card": per_card,
        # raw prompt opportunities: what the surveil prompt itself carries
        # in each view (candidate surfaces may carry card faces)
        "p0_prompt_opportunities": vi_opps(c0)[:3],
        "p1_prompt_opportunities": vi_opps(c1)[:3],
    }
    with open(f"{EVDIR}/surveil_capture.json", "w") as f:
        json.dump(capture, f, indent=1, default=str)
    wire("surveil_capture", {k: (v if k != "per_card" else
                                 [{kk: vv for kk, vv in c.items()
                                   if kk != "p0_raw_object" and kk != "p1_raw_object"
                                   and kk != "true_raw_object"}
                                  for c in v])
                             for k, v in capture.items()})
    say("[capture] wrote surveil_capture.json")
    return capture


async def answer_surveil_keep_top(c):
    """Select every candidate: the SELECTED cards stay on top of the library
    (verified select semantics), i.e. keep all on top."""
    for opp in vi_opps(c):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        ids = [ch.get("id") for ch in cands if ch.get("id") is not None]
        if not ids:
            continue
        iid = opp.get("interactionId")
        wire("surveil_answer", {"who": c.name, "iid": iid, "picked": ids})
        await c.send_interaction({"interactionId": iid,
                                  "response": {"type": "select",
                                               "data": {"choiceIds": ids}}})
        say(f"[{c.name}] surveil answered: keep {len(ids)} on top")
        return True
    return False


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    # THE decision under test: capture visibility BEFORE answering.
    if wtype == "SurveilChoice" and wplayer == 0:
        if not ST["captured"]:
            ST["capture"] = await capture_surveil(c, C1)
            ST["captured"] = True
            ST["surveil_rev"] = (c.latest or {}).get("state_revision")
            return True  # next tick answers
        if not ST["answered"]:
            if await answer_surveil_keep_top(c):
                ST["answered"] = True
                return True
            wire("surveil_noopportunity", {})
            return False
        return False

    # Gaze's "you may return it to hand": decline for a deterministic flow.
    if wtype == "OptionalEffectChoice" and wplayer == 0 and ST["answered"]:
        for opp in vi_opps(c):
            resp = opp.get("response") or {}
            if resp.get("type") != "exactChoices":
                continue
            for ch in (resp.get("data") or {}).get("choices", []) or []:
                vals = [str(s.get("data", {}).get("value", "")).lower()
                        for s in ch.get("surfaces", []) or []]
                if "false" in vals:
                    iid = opp.get("interactionId")
                    cid = ch.get("choiceId") or ch.get("id")
                    wire("optional_decline", {"iid": iid, "cid": cid})
                    await c.send_interaction(
                        {"interactionId": iid,
                         "response": {"type": "choose",
                                      "data": {"choiceId": cid}}})
                    say("[P0] declined Gaze return-to-hand")
                    return True
        return False

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand(state, pid)) - 7)
        picks = hand(state, pid)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)}")
            return True
        return False

    if wtype not in ("Priority", None) and wplayer == 0:
        wire("p0_hold", {"wtype": wtype})
        return False

    if is_my_main(state, pid):
        hid = find_hand(state, pid, ISLAND)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand/Island"})
                await c.send_action(a)
                return True
        if not ST["answered"]:
            oid = find_hand(state, pid, GAZE)
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == oid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Gaze",
                                          "object_id": oid})
                    await c.send_action(a)
                    say("[P0] casts Otherworldly Gaze")
                    return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand(state, pid)) - 7)
        picks = hand(state, pid)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False
    if is_my_main(state, pid):
        hid = find_hand(state, pid, ISLAND)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


C1 = None


async def main():
    global C0, C1
    c0 = PhaseClient("P0")
    await c0.connect()
    C0 = c0
    sess = await c0.create(P0_DECK, player_count=2)
    game_code = sess["game_code"]
    say(f"game {game_code}; P0 seat {c0.player_id}")
    wire("game_created", {"game_code": game_code})

    c1 = PhaseClient("P1")
    await c1.connect()
    await c1.join(game_code, P1_DECK)
    C1 = c1
    say(f"P1 joined; seat {c1.player_id}")

    # let GameStarted snapshots arrive
    await asyncio.sleep(2)

    last_progress = time.time()
    while time.time() - START < DEADLINE and not ST["stop"]:
        acted0 = await tick_p0(c0, 0)
        acted1 = await tick_p1(c1, 1)
        if acted0 or acted1:
            last_progress = time.time()
        # completion: surveil answered and choice no longer pending and
        # stack empty -> export post and stop
        if ST["answered"]:
            s0, _, wf0 = cur(c0)
            if s0 is not None and (wf0.get("type") not in
                                   ("SurveilChoice", "OptionalEffectChoice")) \
                    and not stack(s0):
                # make sure the game actually advanced past the cast
                await asyncio.sleep(1)
                s0b, _, wf0b = cur(c0)
                if s0b is not None and not stack(s0b) and \
                        wf0b.get("type") not in ("SurveilChoice", "OptionalEffectChoice"):
                    say("[main] surveil resolved, stack empty -> post export")
                    break
        if time.time() - last_progress > 120:
            say("[main] 120s without progress -> stop")
            wire("watchdog", {"idle_seconds": 120})
            break
        await asyncio.sleep(0.15)

    post = await do_export("post.json")

    # ---------------- assertions ----------------
    A = {}
    notes = {}
    cap = ST["capture"]
    A["A1_setup_ok"] = "passed" if (cap and len(cap.get("surveilled_ids", [])) >= 1) \
        else "failed"
    notes["A1_setup_ok"] = (f"SurveilChoice reached for P0; "
                            f"{len(cap['surveilled_ids']) if cap else 0} card ids captured"
                            if cap else "SurveilChoice never observed")

    if cap:
        cards = cap["per_card"]
        # A2: surveilling player sees the true names
        bad2 = [r for r in cards
                if r["p0_view"]["name"] != r["true"]["name"]]
        A["A2_surveiller_sees"] = "passed" if not bad2 else "failed"
        notes["A2_surveiller_sees"] = (
            "all surveilled cards face-up in P0 view"
            if not bad2 else
            f"P0 view hid {len(bad2)} card(s): "
            + ", ".join(f"{r['object_id']} shown as {r['p0_view']['name']!r}"
                        for r in bad2))
        # A3: opponent does not see the true names; and P1's waiting_for
        # payload must not carry the unredacted cards ids
        bad3 = [r for r in cards
                if r["p1_view"]["name"] is not None
                and r["p1_view"]["name"] == r["true"]["name"]]
        leaked_ids = []
        wf1d = (cap.get("waiting_for_p1") or {}).get("data") or {}
        for k in ("cards", "card_ids", "cardIds"):
            v = wf1d.get(k)
            if isinstance(v, list):
                leaked_ids += [str(x) for x in v
                               if str(x) in cap["surveilled_ids"]]
        A["A3_opponent_blind"] = ("passed"
                                  if (not bad3 and not leaked_ids) else "failed")
        notes["A3_opponent_blind"] = (
            "P1 view hides all surveilled cards; no id leak in P1 payload"
            if (not bad3 and not leaked_ids) else
            f"P1 leaks: names={[(r['object_id'], r['p1_view']['name']) for r in bad3]} "
            f"payload_ids={leaked_ids}")
    else:
        A["A2_surveiller_sees"] = "not-run"
        A["A3_opponent_blind"] = "not-run"
        notes["A2_surveiller_sees"] = "no capture (A1 failed)"
        notes["A3_opponent_blind"] = "no capture (A1 failed)"

    s0f, _, _ = cur(c0)
    gaze_zone = None
    if s0f is not None:
        for oid, o in objs(s0f).items():
            if lname(o) == GAZE.lower() and o.get("controller") == 0:
                gaze_zone = o.get("zone")
                break
    A["A4_choice_completes"] = "passed" if (ST["answered"] and gaze_zone in
                                            ("Graveyard", "Hand")) else "failed"
    notes["A4_choice_completes"] = (f"surveil answered={ST['answered']}; "
                                    f"Gaze zone={gaze_zone}")
    A["A5_cleanup"] = "passed" if (s0f is not None and not stack(s0f)) else "failed"
    notes["A5_cleanup"] = (f"post stack empty={s0f is not None and not stack(s0f)}; "
                           f"phase={s0f.get('phase') if s0f else None}")

    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif A["A2_surveiller_sees"] == "failed" or A["A3_opponent_blind"] == "failed":
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "run_id": RUN_ID,
        "issue": 7362,
        "server": SERVER_IDENTITY,
        "server_hello": {"server_version": "0.99.0", "build_commit": "d919616",
                         "protocol_version": 98},
        "game_code": game_code,
        "ports": {"server": 9375},
        "verdict": verdict,
        "assertions": A,
        "scope": ("Surveil visibility: SurveilChoice pending for P0 via "
                  "Otherworldly Gaze; per-viewer redaction check on the native "
                  "engine, two human-client seats, protocol-98 driver."),
        "limitations": [
            "Browser UI not exercised; the visibility check is on the "
            "engine's per-viewer StateUpdate projection, which the frontend "
            "consumes via display_visible_to_viewer.",
            "The prebuilt phase-server has no standalone state-restore; "
            "states are authoritative exports restorable only via full game "
            "replay (scenario_7362.py).",
            "Single surveil event (Otherworldly Gaze, surveil 3); other "
            "surveil sources not exercised.",
        ],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(START)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes,
                   "verdict": verdict,
                   "capture_summary": {k: v for k, v in (cap or {}).items()
                                       if k != "per_card"}}, f, indent=1,
                  default=str)
    say("assertions: " + json.dumps(A))
    say("verdict: " + verdict)

    await c0.close()
    await c1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
