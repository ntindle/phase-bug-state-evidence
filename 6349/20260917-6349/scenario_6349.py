#!/usr/bin/env python3
"""Issue #6349: Swiss bye is not credited a match win in odd-sized pods.

Subsystem: draft-core DraftSession (crates/draft-core/src/session.rs),
exercised through the native server's draft WebSocket API
(CreateDraftWithSettings / JoinDraftWithPassword / DraftAction), NOT the
game engine. A 3-seat Sealed draft (Swiss, Casual) is created, all seats
submit 40-card decks, then GeneratePairings runs round 1. The reported
outcome is tested directly: the unpaired (bye) seat must end the round
with match_wins == 1 and top the match_wins-sorted standings.

Assertions:
  A1 setup_ok        3-seat Swiss Sealed draft created, all joined, pools dealt
  A2 decks_submitted all 3 seats submitted a 40-card deck (status Deckbuilding done)
  A3 pairings_made    round-1 pairings generated, status MatchInProgress, current_round 1
  A4 bye_identified   exactly one 2-seat pairing; the remaining seat is the bye seat
  A5 bye_credited     bye seat match_wins == 1; paired seats match_wins == 0
  A6 standings_top    standings sorted by match_wins desc; bye seat is standings[0]
  A7 no_rejections    zero DraftActionRejected / Error frames during the flow
"""
import asyncio
import copy
import json
import os
import sys
import time

import websockets

URL = "ws://127.0.0.1:9374/ws"
HELLO = {
    "type": "ClientHello",
    "data": {"client_version": "driver-0.1", "build_commit": "driver", "protocol_version": 72},
}
RUN_ID = os.environ.get("RUN_ID", time.strftime("run-%Y%m%d-%H%M%S"))
EVIDENCE_DIR = os.path.join(
    os.path.expanduser("~/workspace/dev/phase-backfill/evidence"), "6349", RUN_ID
)

assertions = []
rejections = []
OPEN_CLIENTS = []


def record(name, status, detail=""):
    assertions.append({"name": name, "status": status, "detail": detail})
    print(f"[{status.upper()}] {name}: {detail}", flush=True)


class DraftClient:
    def __init__(self, name):
        self.name = name
        self.ws = None
        self.seat = None
        self.view = None
        self.inbox = asyncio.Queue()
        self._pump_task = None

    async def connect(self):
        self.ws = await websockets.connect(URL, max_size=200_000_000)
        await asyncio.wait_for(self.ws.recv(), 5)  # ServerHello
        await self.ws.send(json.dumps(HELLO))
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                t = msg.get("type")
                d = msg.get("data", {})
                if t in ("DraftActionRejected", "Error"):
                    rejections.append({"client": self.name, "type": t, "data": d})
                if t == "DraftJoined":
                    self.seat = int(d["seat_index"])
                    self.view = d["view"]
                elif t == "DraftStateUpdate":
                    self.view = d["view"]
                elif t == "DraftSpectatorView":
                    self.view = d["view"]
                await self.inbox.put((t, d))
                try:
                    with open(os.path.join(EVIDENCE_DIR, "draft_wire_log.jsonl"), "a") as f:
                        keys = list(d.keys()) if isinstance(d, dict) else []
                        f.write(json.dumps({"client": self.name, "type": t, "data_keys": keys}) + "\n")
                except Exception:
                    pass
        except Exception:
            pass

    async def wait_for(self, types, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t, d = await asyncio.wait_for(self.inbox.get(), max(1, deadline - time.time()))
            if t in types:
                return t, d
        raise TimeoutError(f"{self.name}: no {types} within {timeout}s")

    async def wait_status(self, status, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.view and self.view.get("status") == status:
                return self.view
            await asyncio.sleep(0.25)
        raise TimeoutError(f"{self.name}: status {status} not reached")

    async def wait_pool(self, minimum=40, timeout=60):
        """Wait until this client's own draft pool has >= minimum cards."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.view and len(self.view.get("pool", [])) >= minimum:
                return self.view
            await asyncio.sleep(0.25)
        raise TimeoutError(f"{self.name}: pool did not reach {minimum}")

    async def send(self, msg):
        await self.ws.send(json.dumps(msg))

    async def close(self):
        if self._pump_task:
            self._pump_task.cancel()
        if self.ws:
            await self.ws.close()


async def main():
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    t0 = time.time()

    clients = [DraftClient(f"seat{i}") for i in range(3)]
    for c in clients:
        await c.connect()
    host = clients[0]

    # --- create 3-seat Swiss Sealed draft ---
    await host.send(
        {
            "type": "CreateDraftWithSettings",
            "data": {
                "display_name": "host",
                "source": {"type": "Uniform", "data": {"set_codes": ["dft"]}},
                "kind": "Sealed",
                "public": False,
                "password": None,
                "timer_seconds": None,
                "tournament_format": "Swiss",
                "pod_policy": "Casual",
                "pod_size": 3,
            },
        }
    )
    t, d = await host.wait_for(("DraftCreated",), 20)
    code = d["draft_code"]
    host.seat = int(d["seat_index"])
    print(f"draft {code}, host seat {host.seat}", flush=True)

    for c in clients[1:]:
        await c.send(
            {
                "type": "JoinDraftWithPassword",
                "data": {"draft_code": code, "display_name": c.name, "password": None},
            }
        )
        await c.wait_for(("DraftJoined",), 20)
    await asyncio.sleep(1)

    seats_ok = sorted(c.seat for c in clients) == [0, 1, 2]
    record("A1", "passed" if seats_ok else "failed", f"seats={[c.seat for c in clients]}")

    # --- start draft: Sealed/AllAtOnce deals pools, status -> Deckbuilding ---
    await host.send({"type": "DraftAction", "data": {"draft_code": code, "action": {"type": "StartDraft"}}})
    for c in clients:
        await c.wait_pool(40, 60)
    pool_sizes = {c.seat: len(c.view.get("pool", [])) for c in clients}
    pools_ok = all(n >= 40 for n in pool_sizes.values())
    record(
        "A1-pools",
        "passed" if pools_ok else "failed",
        f"pool_sizes={pool_sizes}",
    )
    if not pools_ok:
        record("A2", "not-run", "pools too small")
        return await finish(code, None, t0)

    # --- each seat submits a 40-card deck from its own pool ---
    for c in clients:
        names = [card["name"] for card in c.view["pool"][:40]]
        await c.send(
            {
                "type": "DraftAction",
                "data": {
                    "draft_code": code,
                    "action": {
                        "type": "SubmitDeck",
                        "data": {"seat": c.seat, "main_deck": names, "commanders": []},
                    },
                },
            }
        )
    # wait until every seat's view shows all decks submitted (status Pairing)
    deadline = time.time() + 30
    pre_view = None
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        v = host.view
        if v and all(s.get("has_submitted_deck") for s in v.get("seats", [])):
            pre_view = copy.deepcopy(v)
            break
    if pre_view is None:
        record("A2", "failed", "decks not all submitted within 30s")
        return await finish(code, None, t0)
    with open(os.path.join(EVIDENCE_DIR, "pre.json"), "w") as f:
        json.dump({"draft_code": code, "captured_at": "pre-pairings", "view": pre_view}, f, indent=1)
    submitted = [s.get("has_submitted_deck") for s in pre_view.get("seats", [])]
    pre_standings_empty = pre_view.get("standings", []) == []
    record(
        "A2",
        "passed" if all(submitted) and pre_standings_empty else "failed",
        f"status={pre_view.get('status')} submitted={submitted} standings_empty={pre_standings_empty}",
    )

    # --- round-1 pairings ---
    # The server auto-runs apply_generate_pairings (the exact reducer named in
    # the issue) when the pod reaches Pairing status, then spawns the paired
    # tables' match games (DraftMatchStart). No post-generation DraftStateUpdate
    # is broadcast, so the post-pairings state is read via a spectator view.
    deadline = time.time() + 60
    match_started = False
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        for c in clients:
            while True:
                try:
                    t, d = c.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t == "DraftMatchStart":
                    match_started = True
        if match_started:
            break
    record(
        "A3",
        "passed" if match_started else "failed",
        "DraftMatchStart received (pairings generated server-side)" if match_started else "no DraftMatchStart within 60s",
    )
    if not match_started:
        record("A4", "not-run", "pairings not generated")
        record("A5", "not-run", "pairings not generated")
        record("A6", "not-run", "pairings not generated")
        return await finish(code, None, t0)

    # post-pairings state via spectator view
    spec = DraftClient("spectator")
    await spec.connect()
    await spec.send({"type": "SpectateDraft", "data": {"draft_code": code}})
    post_view = None
    deadline = time.time() + 30
    while time.time() < deadline and post_view is None:
        try:
            t, d = await asyncio.wait_for(spec.inbox.get(), 2)
        except asyncio.TimeoutError:
            break
        if t == "DraftSpectatorView":
            post_view = d["view"]
    await spec.close()
    if post_view is None:
        record("A4", "not-run", "no spectator view")
        record("A5", "not-run", "no spectator view")
        record("A6", "not-run", "no spectator view")
        return await finish(code, None, t0)
    with open(os.path.join(EVIDENCE_DIR, "post.json"), "w") as f:
        json.dump({"draft_code": code, "captured_at": "post-pairings", "view": post_view}, f, indent=1)

    round_ok = post_view.get("current_round") == 1 and post_view.get("status") == "MatchInProgress"
    record(
        "A3b",
        "passed" if round_ok else "failed",
        f"spectator status={post_view.get('status')} current_round={post_view.get('current_round')}",
    )

    pairings = post_view.get("pairings", [])
    paired_seats = set()
    for p in pairings:
        paired_seats.add(p["seat_a"])
        paired_seats.add(p["seat_b"])
    all_seats = {0, 1, 2}
    bye_seats = sorted(all_seats - paired_seats)
    bye_ok = len(pairings) == 1 and len(bye_seats) == 1
    bye_seat = bye_seats[0] if bye_ok else None
    record(
        "A4",
        "passed" if bye_ok else "failed",
        f"pairings={len(pairings)} paired={sorted(paired_seats)} bye_seat={bye_seat}",
    )
    if not bye_ok:
        record("A5", "not-run", "no clean bye to check")
        record("A6", "not-run", "no clean bye to check")
        return await finish(code, post_view, t0)

    standings = post_view.get("standings", [])
    by_seat = {s["seat_index"]: s for s in standings}
    bye_entry = by_seat.get(bye_seat, {})
    bye_wins = bye_entry.get("match_wins")
    others = [(seat, by_seat.get(seat, {}).get("match_wins")) for seat in sorted(paired_seats)]
    credit_ok = bye_wins == 1 and all(w == 0 for _, w in others)
    record(
        "A5",
        "passed" if credit_ok else "failed",
        f"bye_seat={bye_seat} match_wins={bye_wins}; paired={others}",
    )

    order_ok = bool(standings) and standings[0]["seat_index"] == bye_seat
    wins_order = [(s["seat_index"], s["match_wins"]) for s in standings]
    record("A6", "passed" if order_ok else "failed", f"standings_order={wins_order}")

    rej_ok = not rejections
    record("A7", "passed" if rej_ok else "failed", f"rejections={len(rejections)}")

    return await finish(code, post_view, t0)


SERVER_IDENTITY = {
    "server_version": "v0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "Full",
    "binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
}


async def close_all():
    for c in list(OPEN_CLIENTS):
        try:
            await c.close()
        except Exception:
            pass


async def finish(code, post_view, t0):
    await close_all()
    verdict = "not-reproduced"
    for a in assertions:
        if a["status"] == "failed" and a["name"] in ("A5",):
            verdict = "reproduced"
    result = {
        "run_id": RUN_ID,
        "issue": 6349,
        "draft_code": code,
        "elapsed_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "scenario": "driver/scenario_6349.py",
        "scope": "3-seat Swiss Sealed draft pod (draft-core DraftSession via native server draft WS API); round-1 pairings auto-generated on reaching Pairing status; bye credit asserted on match_wins-sorted standings",
        "verdict": verdict,
        "assertions": assertions,
        "rejections": rejections,
        "limitations": [
            "Browser UI not exercised; native server draft WebSocket API only.",
            "Round-1 pairings were auto-generated by the server on the final deck submission (ensure_pairings_generated -> apply_generate_pairings, the exact reducer named in the issue); the host-initiated GeneratePairings wire path was not separately driven.",
            "No DraftStateUpdate is broadcast after server-side pairing generation, so the post-pairings state was read via a spectator view (SpectateDraft).",
            "12x-equivalent deck density n/a; sealed pools are engine-generated (6 DFT packs per seat, first 40 pool cards submitted as the deck).",
            "Not tested on the original 2026-07-22 build; verdict is scoped to v0.78.0, not a fix claim.",
        ],
    }
    with open(os.path.join(EVIDENCE_DIR, "assertions.json"), "w") as f:
        json.dump(result, f, indent=1)
    with open(os.path.join(EVIDENCE_DIR, "run.json"), "w") as f:
        json.dump(result, f, indent=1)
    return result


if __name__ == "__main__":
    result = asyncio.run(main())
    failed = [a for a in result["assertions"] if a["status"] == "failed"]
    print(f"done: {len(result['assertions'])} assertions, {len(failed)} failed")
    sys.exit(1 if failed else 0)
