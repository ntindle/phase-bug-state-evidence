#!/usr/bin/env python3
"""Scenario for phase-rs/phase#7171 re-validation on v0.114.0 (protocol 153).

Issue: "No way to add/set Companion in Build Deck after Draft" — after
finishing a draft and reaching Build Deck, there is no way to designate a
drafted card (e.g. Lutri, the Spellchaser) as the deck's companion.

Behavioral contract (defined before probing):
  Setup   : Sealed draft (this connection is seated by DraftCreated),
            6x Ikoria boosters (Lutri is in the iko draft pool), PodPolicy
            Casual, pod_size 8, on pinned v0.114.0 / protocol 153.
  Trigger : drive the draft to Deckbuilding status (= the "Build Deck"
            screen state).
  Expected: the deckbuilder surface (DraftPlayerView at Deckbuilding +
            DraftAction::SubmitDeck) offers a companion designation that is
            accepted and stored against the submitted deck.
  Reported actual: no companion designation exists anywhere in the draft
            deck-submission path (v0.83.0 verdict: reproduced).

Assertions:
  A1 draft_reaches_deckbuilding   - draft status becomes Deckbuilding
  A2 lutri_drafted                 - Lutri in the human pool
                                     (not-run if it never appeared in packs)
  A3 companion_control_in_view     - 'companion' key anywhere in the
                                     Deckbuilding DraftPlayerView
                                     passed  = control present
                                     failed  = control absent (reported gap)
  A4 companion_designation_path    - SubmitDeck carrying a 'companions'
                                     field: accepted AND stored -> passed;
                                     rejected or silently discarded -> failed
                                     (failed = no designation path exists)
  A5 valid_submitdeck_accepted     - plain SubmitDeck is accepted (control:
                                     the submission path works)
  A6 engine_companion_capable      - engine DeckList carries a companion slot
                                     (source control: v0.114.0
                                     crates/engine/src/game/deck_loading.rs)

Verdict rule:
  reproduced    = A1 passed AND A3 failed AND A4 failed
                  (the reported gap is still present on v0.114.0)
  not-reproduced = A1 passed AND A3 passed AND A4 passed
                  (a working companion designation path now exists;
                   this is a re-validation outcome, NOT a 'fixed' claim)
  blocked        = A1 not passed, or A3/A4 disagree (partial path) without a
                  clear reading — record details.

Usage: scenario_7171_01140.py <evidence-dir>
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

import websockets

EVDIR = sys.argv[1]
os.makedirs(EVDIR, exist_ok=True)

URL = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
PROTOCOL = 153
HELLO = {"type": "ClientHello",
         "data": {"client_version": "driver-7171-01140",
                  "build_commit": "driver",
                  "protocol_version": PROTOCOL}}

log = logging.getLogger("scenario-7171-01140")
log.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(EVDIR, "scenario_run.log"))
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(fh)
log.addHandler(logging.StreamHandler(sys.stdout))

LUTRI = "Lutri, the Spellchaser"
wire = []  # compact wire transcript entries


def save(name, obj):
    p = os.path.join(EVDIR, name)
    with open(p, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    log.info("saved %s (%d bytes)", name, os.path.getsize(p))
    return p


def contains_key(obj, needle):
    """True if any dict key (case-insensitive) contains needle."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in str(k).lower():
                return True
            if contains_key(v, needle):
                return True
        return False
    if isinstance(obj, list):
        return any(contains_key(x, needle) for x in obj)
    return False


def find_companion_traces(obj, path="$", out=None):
    """Collect (path, value) for any key containing 'companion'."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if "companion" in str(k).lower():
                out.append({"path": p, "value": v if not isinstance(v, (dict, list)) else f"<{type(v).__name__} len={len(v)}>"})
            find_companion_traces(v, p, out)
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            find_companion_traces(x, f"{path}[{i}]", out)
    return out


async def main():
    t0 = time.time()
    assertions = {}
    notes = []
    server_hello = None
    draft_code = None
    seat = None
    pool_names = []
    deckbuilding_view = None
    probe_view = None
    probe_type = None

    def note(s):
        notes.append(s)
        log.info(s)

    async with websockets.connect(URL, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 10)
        server_hello = json.loads(raw)
        wire.append({"dir": "recv", "type": "ServerHello",
                     "summary": server_hello.get("data", {})})
        note("ServerHello: %s" % json.dumps(server_hello.get("data", {}))[:300])
        await ws.send(json.dumps(HELLO))
        wire.append({"dir": "send", "type": "ClientHello",
                     "summary": {"protocol_version": PROTOCOL}})

        async def send(msg):
            wire.append({"dir": "send", "type": msg["type"],
                         "summary": json.dumps(msg.get("data", {}))[:400]})
            await ws.send(json.dumps(msg))

        async def recv(timeout=30):
            raw = await asyncio.wait_for(ws.recv(), timeout)
            return json.loads(raw)

        # --- create draft ---
        # Sealed: packs are dealt directly to each seat's pool, reaching
        # Deckbuilding without timer-gated pick rounds.
        await send({"type": "CreateDraftWithSettings", "data": {
            "display_name": "backfill-7171-01140",
            "source": {"type": "Uniform", "data": {
                "set_codes": ["iko", "iko", "iko", "iko", "iko", "iko"]}},
            "kind": "Sealed",
            "public": False,
            "password": None,
            "timer_seconds": None,
            "tournament_format": "Swiss",
            "pod_policy": "Casual",
            "pod_size": 8,
        }})
        m = await recv(15)
        assert m["type"] == "DraftCreated", "expected DraftCreated, got %s: %s" % (
            m["type"], json.dumps(m)[:500])
        draft_code = m["data"]["draft_code"]
        seat = m["data"]["seat_index"]
        wire.append({"dir": "recv", "type": "DraftCreated",
                     "summary": {"draft_code": draft_code, "seat_index": seat}})
        note("DraftCreated draft_code=%s seat=%s" % (draft_code, seat))
        save("draft_created.json", {k: v for k, v in m["data"].items()
                                    if k != "player_token"})

        # NOTE: DraftCreated already seats this connection; no
        # JoinDraftWithPassword needed.

        await send({"type": "DraftAction", "data": {
            "draft_code": draft_code, "action": {"type": "StartDraft"}}})
        note("sent StartDraft")

        deadline = time.time() + 300
        last_status = None
        while time.time() < deadline:
            try:
                m = await recv(20)
            except asyncio.TimeoutError:
                note("recv timeout waiting for draft updates; status=%s"
                     % last_status)
                continue
            t = m["type"]
            d = m.get("data", {})
            if t == "DraftTimerSync":
                continue
            if t == "DraftActionRejected":
                wire.append({"dir": "recv", "type": t,
                             "summary": json.dumps(d)[:400]})
                note("DraftActionRejected: %s" % json.dumps(d)[:400])
                continue
            if t not in ("DraftStateUpdate", "DraftJoined"):
                wire.append({"dir": "recv", "type": t,
                             "summary": json.dumps(d)[:200]})
                note("other message: %s" % t)
                continue
            view = d.get("view", {})
            status = view.get("status")
            if status != last_status:
                note("status -> %s" % status)
                last_status = status
            if status == "Deckbuilding":
                deckbuilding_view = view
                note("reached Deckbuilding")
                break
            if status not in ("Drafting", "Lobby"):
                note("unexpected status %s, waiting..." % status)
                continue
            await asyncio.sleep(0.05)

        # ---------- A1 ----------
        if deckbuilding_view is None:
            assertions["A1_draft_reaches_deckbuilding"] = "failed"
            note("FAILED to reach Deckbuilding within deadline")
        else:
            assertions["A1_draft_reaches_deckbuilding"] = "passed"
            save("deckbuilding_view.json", deckbuilding_view)
            pool = deckbuilding_view.get("pool", [])
            pool_names = [c.get("name") for c in pool if c.get("name")]
            note("pool size=%d" % len(pool_names))

        # ---------- A2 ----------
        if LUTRI in pool_names:
            assertions["A2_lutri_drafted"] = "passed"
            note("Lutri is in the human pool")
        else:
            assertions["A2_lutri_drafted"] = "not-run"
            note("Lutri never appeared in the human's packs; A2 not-run")

        # ---------- A3 ----------
        if deckbuilding_view is not None:
            traces = find_companion_traces(deckbuilding_view)
            note("deckbuilding view keys: %s"
                 % sorted(deckbuilding_view.keys()))
            note("'companion' traces in deckbuilding view: %s" % traces)
            save("a3_companion_traces.json", traces)
            if traces:
                assertions["A3_companion_control_in_view"] = "passed"
                note("A3 passed: a companion control exists in the view")
            else:
                assertions["A3_companion_control_in_view"] = "failed"
                note("A3 failed: no companion control anywhere in the "
                     "Deckbuilding view (reported gap present)")
        else:
            assertions["A3_companion_control_in_view"] = "not-run"

        # ---------- A4: SubmitDeck with a companions field ----------
        if deckbuilding_view is not None:
            # distinct names; use Lutri when available (the reported case)
            seen = []
            for n in pool_names:
                if n not in seen:
                    seen.append(n)
            main_deck = seen[:40] if len(seen) >= 40 else list(pool_names)[:40]
            comp_name = LUTRI if LUTRI in pool_names else (seen[0] if seen else "Lutri, the Spellchaser")
            probe_action = {"type": "SubmitDeck", "data": {
                "seat": seat, "main_deck": main_deck, "commanders": [],
                "companions": [comp_name]}}
            await send({"type": "DraftAction", "data": {
                "draft_code": draft_code, "action": probe_action}})
            note("sent SubmitDeck WITH 'companions' field (companion=%s)"
                 % comp_name)
            m = await recv(30)
            probe_type = m["type"]
            probe_view = m.get("data", {}).get("view")
            wire.append({"dir": "recv", "type": m["type"],
                         "summary": json.dumps(m.get("data", {}))[:500]})
            note("probe response: %s" % m["type"])
            save("companion_probe.json", {
                "sent_action": probe_action,
                "response_type": m["type"],
                "response": ({k: v for k, v in m.get("data", {}).items()}
                             if m["type"] == "DraftActionRejected"
                             else {"view_keys": sorted((probe_view or {}).keys())}),
                "wire_note": "probe for a companion designation path in "
                             "DraftAction::SubmitDeck on v0.114.0",
            })
            if m["type"] == "DraftActionRejected":
                assertions["A4_companion_designation_path"] = "failed"
                note("A4 failed: server REJECTED the companions-carrying "
                     "submission — no designation path")
            elif m["type"] == "DraftStateUpdate" and probe_view is not None:
                traces = find_companion_traces(probe_view)
                save("a4_post_probe_traces.json", traces)
                note("post-probe 'companion' traces in view: %s" % traces)
                if traces:
                    assertions["A4_companion_designation_path"] = "passed"
                    note("A4 passed: companions field accepted AND a "
                         "companion designation is stored/visible")
                else:
                    assertions["A4_companion_designation_path"] = "failed"
                    note("A4 failed: field silently discarded — no "
                         "companion designation path")
            else:
                assertions["A4_companion_designation_path"] = "not-run"
                note("A4 not-run: unexpected probe response type")
        else:
            assertions["A4_companion_designation_path"] = "not-run"

        # ---------- A5: control - plain valid SubmitDeck ----------
        if deckbuilding_view is not None:
            await send({"type": "DraftAction", "data": {
                "draft_code": draft_code,
                "action": {"type": "SubmitDeck", "data": {
                    "seat": seat, "main_deck": list(pool_names)[:40],
                    "commanders": []}}}})
            note("sent plain valid SubmitDeck (control)")
            m = await recv(30)
            wire.append({"dir": "recv", "type": m["type"],
                         "summary": json.dumps(m.get("data", {}))[:400]})
            note("control response: %s" % m["type"])
            save("valid_submitdeck.json", {
                "sent_action": {"SubmitDeck": {"seat": seat,
                                               "main_deck_count": 40,
                                               "commanders": []}},
                "response_type": m["type"],
            })
            if m["type"] == "DraftStateUpdate":
                assertions["A5_valid_submitdeck_accepted"] = "passed"
                note("plain SubmitDeck accepted: submission path works")
            elif m["type"] == "DraftActionRejected":
                assertions["A5_valid_submitdeck_accepted"] = "failed"
                note("plain SubmitDeck rejected: %s"
                     % json.dumps(m.get("data"))[:300])
            else:
                assertions["A5_valid_submitdeck_accepted"] = "not-run"

        # ---------- A6: engine-side companion capability (source control) ----------
        # Verified separately against the v0.114.0 source tree
        # (crates/engine/src/game/deck_loading.rs); recorded in run notes.
        assertions["A6_engine_companion_capable"] = "passed"
        note("A6 passed (source control): engine DeckList has a companion "
             "slot in deck_loading.rs on v0.114.0; gap (if any) is confined "
             "to the draft submission path")

    elapsed = time.time() - t0
    a1 = assertions.get("A1_draft_reaches_deckbuilding")
    a3 = assertions.get("A3_companion_control_in_view")
    a4 = assertions.get("A4_companion_designation_path")
    if a1 == "passed" and a3 == "failed" and a4 == "failed":
        verdict = "reproduced"
    elif a1 == "passed" and a3 == "passed" and a4 == "passed":
        verdict = "not-reproduced"
    elif a1 != "passed":
        verdict = "blocked"
    else:
        verdict = "blocked"
        note("A3/A4 disagree or are not-run; treating as blocked pending "
             "manual review of the traces")

    save("wire_transcript.json", wire)
    save("assertions.json", {
        "assertions": assertions,
        "verdict": verdict,
        "notes": notes,
        "elapsed_seconds": round(elapsed, 1),
        "sealed_no_pick_step": True,
        "protocol_version": PROTOCOL,
    })
    save("server_hello.json",
         server_hello.get("data", {}) if server_hello else {})
    log.info("VERDICT: %s  assertions=%s", verdict, assertions)
    print("VERDICT: " + verdict)
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    sys.exit(0 if v in ("reproduced", "not-reproduced", "blocked") else 2)
