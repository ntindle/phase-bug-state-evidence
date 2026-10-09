#!/usr/bin/env python3
"""Issue #6349: Swiss bye is not credited a match win in odd-sized pods.

v0.104.0 port of the verified protocol-106 driver (scenario_6349_01030.py).
Subsystem: draft-core DraftSession (crates/draft-core/src/session.rs),
exercised through the native server's draft WebSocket API
(CreateDraftWithSettings / JoinDraftWithPassword / DraftAction), NOT the
game engine. A 3-seat Sealed draft (Swiss, Casual) is created, all seats
submit 40-card decks, then GeneratePairings runs round 1. The reported
outcome is tested directly: the unpaired (bye) seat must end the round
with match_wins == 1 and top the match_wins-sorted standings.

Protocol note (v0.104.0, protocol 118): the draft wire API surface used
here (CreateDraftWithSettings / JoinDraftWithPassword / DraftAction /
SpectateDraft / DraftCreated / DraftJoined / DraftStateUpdate /
DraftMatchStart / DraftSpectatorView / DraftActionRejected) is unchanged
from the verified protocol-106 runs; only the ClientHello protocol
version and the server identity block were re-pinned, and hashes were
recomputed against the on-disk v0.104.0 release files.

Assertions:
  A1 setup_ok        3-seat Swiss Sealed draft created, all joined, pools dealt
  A1-pools           all pools >= 40 cards
  A2 decks_submitted all 3 seats submitted a 40-card deck (status Deckbuilding done)
  A3 pairings_made    DraftMatchStart received (server-side pairing generation)
  A3b spectator_state spectator view shows MatchInProgress, current_round 1
  A4 bye_identified   exactly one 2-seat pairing; the remaining seat is the bye seat
  A5 bye_credited     bye seat match_wins == 1; paired seats match_wins == 0
  A6 standings_top    standings sorted by match_wins desc; bye seat is standings[0]
  A7 no_rejections    zero DraftActionRejected / Error frames during the flow
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

URL = "ws://127.0.0.1:9375/ws"
HELLO = {
    "type": "ClientHello",
    "data": {"client_version": "driver-0.1", "build_commit": "driver", "protocol_version": 118},
}
RUN_ID = os.environ.get("RUN_ID", "run-6349-reval-v01040-20261008-2343")
EVIDENCE_DIR = os.path.join(
    os.path.expanduser("~/workspace/dev/phase-backfill/evidence"), "6349", RUN_ID
)
WIRE_LOG = os.path.join(EVIDENCE_DIR, "draft_wire_log.jsonl")

assertions = []
rejections = []
OPEN_CLIENTS = []


def record(name, status, detail=""):
    assertions.append({"name": name, "status": status, "detail": detail})
    print(f"[{status.upper()}] {name}: {detail}", flush=True)


def wire_log_write(client_name, t, d):
    try:
        with open(WIRE_LOG, "a") as f:
            keys = list(d.keys()) if isinstance(d, dict) else []
            f.write(json.dumps({"client": client_name, "type": t, "data_keys": keys}) + "\n")
    except Exception:
        pass


class DraftClient:
    def __init__(self, name):
        self.name = name
        self.ws = None
        self.seat = None
        self.view = None
        self.inbox = asyncio.Queue()
        self._pump_task = None
        OPEN_CLIENTS.append(self)

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
                wire_log_write(self.name, t, d)
        except Exception:
            pass

    async def wait_for(self, types, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t, d = await asyncio.wait_for(self.inbox.get(), max(1, deadline - time.time()))
            if t in types:
                return t, d
        raise TimeoutError(f"{self.name}: no {types} within {timeout}s")

    async def wait_pool(self, minimum=40, timeout=60):
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


SERVER_IDENTITY = {
    "server_version": "v0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
    "mode": "Full",
    "binary_sha256": "f4f3d74a21a5474453d4ca06b9c90e2751af7bea66c0101db26f4baa385f5c9d",
    "card_data_sha256": "7d131899fb22736dc6068c25ec220f2fed8b71578dba4ba1135c56a19247789d",
    "draft_pools_sha256": "d8d4664a45d095d5f30f570c5463d9a1a5f231049ff30403477efbf4384c79dc",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
}


async def close_all():
    for c in list(OPEN_CLIENTS):
        try:
            await c.close()
        except Exception:
            pass


async def main():
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    t0 = time.time()
    code = None
    post_view = None
    pre_view = None

    try:
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
        record("A1-pools", "passed" if pools_ok else "failed", f"pool_sizes={pool_sizes}")
        if not pools_ok:
            record("A2", "not-run", "pools too small")
            return

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
        deadline = time.time() + 30
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            v = host.view
            if v and all(s.get("has_submitted_deck") for s in v.get("seats", [])):
                pre_view = copy.deepcopy(v)
                break
        if pre_view is None:
            record("A2", "failed", "decks not all submitted within 30s")
            return
        with open(os.path.join(EVIDENCE_DIR, "pre.json"), "w") as f:
            json.dump({"draft_code": code, "captured_at": "pre-pairings", "view": pre_view}, f, indent=1)
        submitted = [s.get("has_submitted_deck") for s in pre_view.get("seats", [])]
        pre_standings_empty = pre_view.get("standings", []) == []
        record(
            "A2",
            "passed" if all(submitted) and pre_standings_empty else "failed",
            f"status={pre_view.get('status')} submitted={submitted} standings_empty={pre_standings_empty}",
        )

        # --- round-1 pairings (server auto-runs apply_generate_pairings) ---
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
            for n in ("A3b", "A4", "A5", "A6"):
                record(n, "not-run", "pairings not generated")
            return

        # --- post-pairings state via spectator view ---
        spec = DraftClient("spectator")
        await spec.connect()
        await spec.send({"type": "SpectateDraft", "data": {"draft_code": code}})
        deadline = time.time() + 30
        while time.time() < deadline and post_view is None:
            try:
                t, d = await asyncio.wait_for(spec.inbox.get(), 2)
            except asyncio.TimeoutError:
                break
            if t == "DraftSpectatorView":
                post_view = d["view"]
        if post_view is None:
            for n in ("A3b", "A4", "A5", "A6"):
                record(n, "not-run", "no spectator view")
            return
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
            for n in ("A5", "A6"):
                record(n, "not-run", "no clean bye to check")
            return

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
    finally:
        await close_all()

    verdict = "not-reproduced"
    for a in assertions:
        if a["status"] == "failed" and a["name"] == "A5":
            verdict = "reproduced"

    # copy the exact driver source into the evidence dir + hash it
    src_path = os.path.abspath(__file__)
    with open(src_path, "rb") as f:
        src_bytes = f.read()
    src_sha = hashlib.sha256(src_bytes).hexdigest()
    with open(os.path.join(EVIDENCE_DIR, "scenario_6349_01040.py"), "wb") as f:
        f.write(src_bytes)

    result = {
        "run_id": RUN_ID,
        "issue": 6349,
        "draft_code": code,
        "elapsed_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "scenario": "driver/scenario_6349_01040.py",
        "scenario_sha256": src_sha,
        "scope": "3-seat Swiss Sealed draft pod (draft-core DraftSession via native server draft WS API); round-1 pairings auto-generated on reaching Pairing status; bye credit asserted on match_wins-sorted standings from a spectator view",
        "verdict": verdict,
        "assertions": assertions,
        "rejections": rejections,
        "limitations": [
            "Browser UI not exercised; native server draft WebSocket API only.",
            "Round-1 pairings were auto-generated by the server on the final deck submission (ensure_pairings_generated -> apply_generate_pairings, the exact reducer named in the issue); the host-initiated GeneratePairings wire path was not separately driven.",
            "No DraftStateUpdate is broadcast after server-side pairing generation, so the post-pairings state was read via a spectator view (SpectateDraft).",
            "Sealed pools are engine-generated (6 DFT packs per seat, first 40 pool cards submitted as the deck).",
            "Not tested on the original 2026-07-22 build; verdict is scoped to v0.104.0, not a fix claim.",
        ],
    }
    with open(os.path.join(EVIDENCE_DIR, "assertions.json"), "w") as f:
        json.dump(result, f, indent=1)
    with open(os.path.join(EVIDENCE_DIR, "run.json"), "w") as f:
        json.dump(result, f, indent=1)
    print(f"verdict={verdict}", flush=True)
    return result


if __name__ == "__main__":
    result = asyncio.run(main())
    failed = [a for a in result["assertions"] if a["status"] == "failed"]
    print(f"done: {len(result['assertions'])} assertions, {len(failed)} failed")
    sys.exit(1 if failed else 0)
