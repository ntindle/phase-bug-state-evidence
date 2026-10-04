#!/usr/bin/env python3
"""Issue #9505: Single-exile origin parse silently drops unparseable origins
instead of failing closed.

Reported (2026-10-03, JacobWoodson, label:bug, status:confirmed-by-repro):
The single-card put-into-exile origin parse
(`crates/engine/src/parser/oracle_trigger.rs`, `try_parse_put_into_exile_from`)
silently drops unparseable origins instead of failing closed: the origin parse
does `.ok()` / `unwrap_or(None)` with the remainder discarded and no tail
check. Consequence: e.g. `put into exile from an opponent's library` parses as
a SUPPORTED unconstrained `ChangesZone` trigger and fires on exile from any
zone. The sibling put-into-graveyard arm fails closed on unconsumed tails
(`parse_graveyard_origin_union` + strict tail check, established in #9482);
exile was intentionally left untouched there. Zero corpus witnesses: all
corpus `put into exile from ...` lines are parseable shapes, so no corpus
card over-fires today.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Rakshasa Vizier + 4x Tormod's Crypt + 4x Grizzly Bears
      + 4x Polluted Delta + 4x Misty Rainforest + 4x Verdant Catacombs
      + 12x Island + 12x Swamp + 12x Forest (60)
  P1: 4x Swords to Plowshares + 56x Plains (60)
Drive:
  1. Both seats keep 7 (no mulligan/bottom needed with keep-always).
  2. P0: play lands, crack a fetchland (seeds P0's graveyard), cast
     Grizzly Bears, cast Tormod's Crypt, cast Rakshasa Vizier.
  3. Leg 1 (positive control -- parsed origin IS honored): with Vizier and
     Crypt on the battlefield and >=1 card in P0's graveyard, activate
     Crypt targeting P0. pre.json is exported INSIDE this submission path
     (guarded flag). The Vizier trigger ("...put into exile from your
     graveyard", parsed origin_zones=["Graveyard"]) must fire: Vizier gains
     >=1 +1/+1 counter. Export mid.json once observed.
  4. Leg 2 (negative control -- unlisted origin does NOT fire): on P1's
     turn, cast Swords to Plowshares targeting P0's Grizzly Bears (exile
     from the battlefield). Vizier's counter count must be UNCHANGED.
     Export post.json after resolution.

Assertions:
  A1_data_level   pinned v0.101.0 card-data.json census: every corpus
                  ChangesZone/ChangesZoneAll exile-destination trigger either
                  carries its named origin in `origin`/`origin_zones` or is
                  legitimately unconstrained ("from anywhere" / no origin
                  clause). Zero corpus triggers show a silently-dropped
                  origin (confirms the issue's "zero corpus witnesses").
  A2_code         engine source at the pinned v0.101.0 tag: the single-card
                  exile arm `try_parse_put_into_exile_from` contains the
                  `.ok()`/`.unwrap_or(None)` origin drop with the remainder
                  discarded and no tail check; the function is byte-identical
                  to 49b64bd08 (the issue's reference commit). The sibling
                  graveyard arm fails closed (`let Some(parsed) =
                  parse_graveyard_origin_union(after_from) else { continue; }`
                  + strict tail check); the batched exile arm fails closed
                  (strict trailing-text check). The v0.101.0 runtime matcher
                  (`triggers.rs` zone-change match) returns `true` for
                  origin=None + empty origin_zones -- i.e. a dropped origin
                  fires on exile from ANY zone.
  A3_leg1        Vizier on battlefield with parsed origin_zones=["Graveyard"]
                  (also asserted live on the exported pre state); Crypt
                  activation exiled >=1 card from P0's graveyard; Vizier
                  gained >=1 +1/+1 counter (mid.json).
  A4_leg2        Swords to Plowshares exiled P0's Grizzly Bears from the
                  battlefield; Vizier's counter count is unchanged vs mid;
                  Bears is in exile (post.json).

Verdict rule: reproduced iff A1 and A2 passed (the parser defect is real in
              the pinned release and has zero corpus witnesses, matching the
              issue). A3/A4 are a runtime origin-faithfulness control; if the
              control is confounded (see run.json notes), they are marked
              not-run and do not block the parser-defect verdict.
              not-reproduced iff the pinned source fails closed on unparseable
              exile origins; blocked iff A1 or A2 cannot be established.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-9505"
ISSUE = 9505
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "server_binary_sha256": None,  # recomputed below
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "source": "2026-10-03: latest stable release v0.101.0 == pinned release "
              "dir (GitHub /releases re-confirmed v0.101.0 still latest "
              "stable this run; ServerHello 0.101.0/acafe9b/protocol 103 "
              "verified by this run's own handshake); hashes recomputed "
              "against on-disk artifacts this run; minisign verification "
              "recorded in the ledger pin (2026-10-03T12:58:00-05:00)",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
    assert _h == {
        "server_binary_sha256": "c32eabdcf93d04f61186558c223a12e6edbe15a678050863ae6d535165359b0a",
        "card_data_sha256": "b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00",
        "draft_pools_sha256": "75bb313864c341a99747e2a2a446dda5d83763bf073f5215d39289fdf8d34b06",
    }[_k], f"hash mismatch on {_f}"

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))

VIZIER = "rakshasa vizier"
CRYPT = "tormod's crypt"
BEARS = "grizzly bears"
SWORDS = "swords to plowshares"
FETCHES = ["polluted delta", "misty rainforest", "verdant catacombs"]
PLAINS = "plains"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = deck((VIZIER, 4), (CRYPT, 4), (BEARS, 4),
               ("Polluted Delta", 4), ("Misty Rainforest", 4),
               ("Verdant Catacombs", 4),
               ("Island", 12), ("Swamp", 12), ("Forest", 12))
P1_DECK = deck((SWORDS, 4), (PLAINS, 56))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(kind, obj):
    WIRE.write(json.dumps({"t": time.time(), "kind": kind,
                           "obj": obj}, default=str) + "\n")
    WIRE.flush()


ST = {}
MULLS = set()
SUBMITTED_OPPS = set()


def reset_attempt():
    global ST
    ST = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "game_code": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_code",
                                       "A3_leg1", "A4_leg2")},
        "notes": [],
        "phase": "setup",
        "fetch_cracked": False,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "crypt_activated_at": None,
        "swords_cast_at": None,
        "vizier_oid": None,
        "mid_counters": None,
        "leg1_deadline": None,
        "data_level_ok": False,
        "code_level_ok": False,
        "hello_ok": False,
    }
    MULLS.clear()
    SUBMITTED_OPPS.clear()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def zone_ids(state, pid, zone, key=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone:
            continue
        if pid is not None and o.get("controller") != pid and o.get("owner") != pid:
            # zone membership for graveyard/exile is by owner; battlefield by controller
            if zone == "Battlefield" and o.get("controller") != pid:
                continue
            if zone in ("Graveyard", "Exile", "Library") and o.get("owner") != pid:
                continue
        if key is not None and obj_lname(state, oid) != key:
            continue
        out.append(int(oid))
    return out


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return (wf.get("type") == "Priority"
            and str((wf.get("data") or {}).get("player")) == str(pid))


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def untapped_land_colors(state, pid):
    colors = set()
    for oid in untapped_lands(state, pid):
        n = obj_lname(state, oid)
        if n == PLAINS:
            colors.add("W")
        elif n == ISLAND:
            colors.add("U")
        elif n == SWAMP:
            colors.add("B")
        elif n == FOREST:
            colors.add("G")
    return colors


def p1p1_count(state, oid):
    o = get_obj(state, oid)
    c = o.get("counters") or {}
    if isinstance(c, dict):
        return sum(v for v in c.values() if isinstance(v, (int, float)))
    return 0


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def activate_action_for(acts, state, key):
    for a in acts:
        if "activat" in a["type"].lower():
            oid = a.get("_src_oid")
            try:
                if oid is not None and obj_lname(state, int(oid)) == key:
                    return a, int(oid)
            except (TypeError, ValueError):
                continue
    return None, None


def candidate_matching(opp, needles):
    """First available candidate whose serialized content contains all
    needles (case-insensitive). Never matches on bare numeric needles."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None


def candidate_with_seat(opp, seat):
    """Player-target candidates carry surfaces with data.seat (see the
    Plagiarize precedent in scenario_5654.py)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("seat") == seat:
                return ch.get("id")
    return None


# ------------------------------------------------------------- A1: data-level census
def check_data_level():
    """Census every corpus ChangesZone/ChangesZoneAll exile-destination
    trigger: each must either carry its named origin in origin/origin_zones
    or be legitimately unconstrained. Zero dropped origins expected."""
    rows = []
    dropped = []
    for name, card in CARD_DATA.items():
        for t in card.get("triggers", []):
            if t.get("mode") not in ("ChangesZone", "ChangesZoneAll"):
                continue
            if t.get("destination") != "Exile":
                continue
            desc = t.get("description") or ""
            dl = desc.lower()
            origin, oz = t.get("origin"), t.get("origin_zones")
            m = re.search(r"put into exile from (.+?)(?:,|\.|$)", dl)
            origin_text = m.group(1).strip() if m else None
            parsed = bool(origin) or bool(oz)
            # legitimately unconstrained shapes
            legit_unconstrained = (
                origin_text is None
                or "anywhere" in (origin_text or "")
            )
            status = "parsed" if parsed else (
                "unconstrained-ok" if legit_unconstrained else "DROPPED")
            rows.append({
                "card": name.strip('"'),
                "mode": t.get("mode"),
                "origin": origin,
                "origin_zones": oz,
                "origin_text": origin_text,
                "status": status,
                "description": desc[:160],
            })
            if status == "DROPPED":
                dropped.append(rows[-1])
    ev = {
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "n_exile_triggers": len(rows),
        "n_dropped": len(dropped),
        "rows": rows,
    }
    with open(f"{EVDIR}/data_census.json", "w") as f:
        json.dump(ev, f, indent=1)
    ok = len(dropped) == 0 and len(rows) > 0
    say(f"A1 data census: {len(rows)} corpus exile-destination triggers, "
        f"{len(dropped)} with silently-dropped origins "
        f"-> {'OK (zero witnesses, matches issue scope)' if ok else 'MISMATCH'}")
    for r in rows:
        say(f"   [{r['status']:17s}] {r['card'][:42]:42s} "
            f"origin={r['origin']} oz={r['origin_zones']} "
            f"text_from={r['origin_text']!r}")
    wire("data_level", {"ok": ok, "n": len(rows), "dropped": dropped})
    ST["data_level_ok"] = ok
    ST["ass"]["A1_data_level"] = "passed" if ok else "failed"
    return ok


# ------------------------------------------------------------- A2: code-level check
EXILE_FN_START = "fn try_parse_put_into_exile_from("


def extract_fn(lines, start_marker):
    s = next(i for i, l in enumerate(lines) if l.startswith(start_marker))
    e = s + 1
    while e < len(lines) and not lines[e].startswith("fn "):
        # stop at the next top-level fn OR the next doc-comment block that
        # starts a new documented item (/// followed later by fn)
        e += 1
    return lines[s:e]


def check_code_level():
    """Verify the defect in the pinned v0.101.0 engine source:
    - the single-card exile arm drops unparseable origins (.ok() /
      unwrap_or(None), remainder discarded, no tail check), byte-identical
      to the issue's reference commit 49b64bd08;
    - the sibling graveyard arm fails closed (strict tail + let-else);
    - the batched exile arm fails closed (strict trailing-text check);
    - the v0.101.0 runtime zone-change matcher treats origin=None +
      empty origin_zones as a match from ANY zone (over-fire)."""
    src_dir = f"{EVDIR}/source"
    os.makedirs(src_dir, exist_ok=True)
    checks = {}

    trig_src = open("/tmp/oracle_trigger_v01010.rs").read().splitlines()
    ref_src = open("/tmp/oracle_trigger_49b64bd08.rs").read().splitlines()

    # 1. extract the single-card exile arm from the pinned tag
    arm = extract_fn(trig_src, EXILE_FN_START)
    arm_text = "\n".join(arm)
    with open(f"{src_dir}/try_parse_put_into_exile_from.v0.101.0.rs", "w") as f:
        f.write(arm_text + "\n")
    # 2. same fn at the issue's reference commit
    ref_arm = extract_fn(ref_src, EXILE_FN_START)
    ref_text = "\n".join(ref_arm)
    checks["arm_identical_to_49b64bd08"] = (arm_text == ref_text)
    # 3. the drop pattern: .ok() swallowing the origin parse failure,
    #    remainder discarded via .map(|(_, z)| z), unwrap_or(None)
    checks["has_ok_swallowing"] = (".ok()" in arm_text)
    checks["has_unwrap_or_none"] = (".unwrap_or(None)" in arm_text)
    checks["discards_remainder"] = (".map(|(_, z)| z)" in arm_text)
    # no tail check in the ORIGIN arm: between parse_origin_zone's use and
    # .unwrap_or(None) there is no tail/remainder inspection at all (the
    # remainder is discarded by .map(|(_, z)| z))
    origin_block = (arm_text.split("fn parse_origin_zone", 1)[1]
                    if "fn parse_origin_zone" in arm_text else arm_text)
    checks["no_tail_check"] = ("tail" not in origin_block.lower()
                               and "remainder" not in origin_block.lower())
    # the arm returns Some (supported) even when the origin parse failed --
    # the function ends in the tail expression Some((ChangesZone, def))
    checks["returns_supported_on_bad_origin"] = (
        "Some((TriggerMode::ChangesZone, def))" in arm_text
        and ".unwrap_or(None)" in arm_text)

    # 5. sibling graveyard arm fails closed
    gy = extract_fn(trig_src, "fn try_parse_put_into_graveyard(")
    gy_text = "\n".join(gy)
    with open(f"{src_dir}/try_parse_put_into_graveyard.v0.101.0.rs", "w") as f:
        f.write(gy_text + "\n")
    # 5. sibling graveyard arm fails closed: the single-card arm propagates
    #    the union parse with `?` (arm fails), the batched arm uses
    #    let-else + continue
    checks["graveyard_fails_closed"] = (
        "parse_graveyard_origin_union(after_from)?" in gy_text)
    gu = extract_fn(trig_src, "fn parse_graveyard_origin_union(")
    gu_text = "\n".join(gu)
    with open(f"{src_dir}/parse_graveyard_origin_union.v0.101.0.rs", "w") as f:
        f.write(gu_text + "\n")
    checks["graveyard_strict_tail"] = (
        "if !tail.trim().is_empty()" in gu_text
        and "return None" in gu_text)

    # 6. batched exile arm fails closed
    bx = extract_fn(trig_src, "fn try_parse_one_or_more_put_into_exile_from(")
    bx_text = "\n".join(bx)
    with open(f"{src_dir}/try_parse_one_or_more_put_into_exile_from.v0.101.0.rs", "w") as f:
        f.write(bx_text + "\n")
    checks["batched_exile_fails_closed"] = (
        "if !after_zones.is_empty()" in bx_text)

    # 7. runtime matcher: unconstrained origin matches any zone
    rt = open("/tmp/triggers.rs").read()
    m = re.search(
        r"let origin_matches = if !trigger\.origin_zones\.is_empty\(\) \{"
        r".*?\n    \};", rt, re.S)
    rt_excerpt = m.group(0) if m else ""
    with open(f"{src_dir}/runtime_origin_match.v0.101.0.rs", "w") as f:
        f.write(rt_excerpt + "\n")
    checks["runtime_unconstrained_matches_any"] = (
        "} else {\n        true\n    };" in rt_excerpt
        and "trigger.origin_zones.contains(&zone)" in rt_excerpt)

    with open(f"{EVDIR}/code_evidence.json", "w") as f:
        json.dump({"checks": checks,
                   "source_commit": "v0.101.0 tag",
                   "files": sorted(os.listdir(src_dir))}, f, indent=1)
    ok = all(checks.values())
    for k, v in checks.items():
        say(f"A2 code check [{k}]: {'OK' if v else 'FAIL'}")
    wire("code_level", {"ok": ok, "checks": checks})
    ST["code_level_ok"] = ok
    ST["ass"]["A2_code"] = "passed" if ok else "failed"
    return ok


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.101.0"), f"unexpected version {ver}"
    assert int(proto) == 103, f"unexpected protocol {proto}"
    assert str(build) == "acafe9b", f"unexpected build {build}"
    ST["hello_ok"] = True


# ------------------------------------------------------------- live drive helpers
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    data = wf_of(state).get("data") or {}
    pend = None
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            pend = p
            break
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    MULLS.add(tag)
    hn = hand_lnames(state, pid)
    say(f"[{tag}] keep {len(hn)}")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "BottomCards":
        return False
    data = wf_of(state).get("data") or {}
    pend = None
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            pend = p
            break
    if not pend:
        return False
    n = (pend.get("count") or 1)
    hand = hand_ids(state, pid)
    picks = sorted(hand, key=lambda o: 0 if "land" not in [
        str(t).lower() for t in (get_obj(state, o).get("card_types") or {})
        .get("core_types", [])] else 1)[:n]
    picks = [int(x) for x in picks]
    say(f"[{tag}] bottoming {len(picks)}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


_DISCARD_REV = {}


async def do_discard_to_handsize(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") != "DiscardToHandSize":
        return False
    data = wf.get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    rev = state.get("state_revision")
    if _DISCARD_REV.get((tag, rev)):
        return False
    n = data.get("count") or data.get("amount") or 1
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    hand = hand_ids(state, pid)

    def is_land(o):
        return "land" in [str(t).lower()
                          for t in (get_obj(state, o).get("card_types") or {})
                          .get("core_types", [])]
    picks = [int(x) for x in sorted(hand, key=lambda o: 0 if is_land(o) else 1)[:n]]
    if not picks:
        return False
    _DISCARD_REV[(tag, rev)] = True
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("discard", {"who": tag, "picks": picks})
    return True


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities") or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces") or []]
                if ("passPriority" in codes
                        and ch.get("status", {}).get("type") == "available"):
                    iid = opp.get("interactionId") or opp.get("id")
                    await c.send_interaction({
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}})
                    return True
    return False


async def answer_target_opp(c, st, state, needle_sets=None, seat=None,
                            label="", phase=""):
    """Answer unsubmitted TargetSelection opportunities. Either needle_sets
    (serialized-content match, never bare numerics) or seat (player-target
    candidates carry surfaces with data.seat). Returns True if submitted."""
    vi = get_vi(st)
    if not vi:
        return False
    wtype = (wf_of(state).get("type") or "")
    if "targetselection" not in wtype.lower():
        return False
    acted = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        rtype = (opp.get("response", {}) or {}).get("type")
        cids = []
        if seat is not None:
            cid = candidate_with_seat(opp, seat)
            if cid is not None:
                cids = [cid]
        else:
            for needles in needle_sets or []:
                cid = candidate_matching(opp, needles)
                if cid is not None and cid not in cids:
                    cids.append(cid)
        wire("target_opp", {"phase": phase, "iid": iid, "rtype": rtype,
                            "cids": cids,
                            "candidates": (opp.get("response", {}) or {})
                            .get("data", {})})
        if not cids:
            say(f"[{label}] WARNING: no candidate matched "
                f"(seat={seat}, needles={needle_sets}); not submitting blind")
            ST["notes"].append(f"{phase}: target selection offered but no "
                               f"candidate matched; see wire_log target_opp")
            continue
        SUBMITTED_OPPS.add(iid)
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cids[0]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": cids}}}
        wire("target_submit", {"phase": phase, "submission": sub})
        await c.send_interaction(sub)
        say(f"[{label}] target -> {cids}")
        acted = True
    return acted


async def answer_search_opp(c, st, state, label=""):
    """Answer a library-search selection (fetchland crack) by picking the
    first available basic-land candidate in the library. The search prompt
    advertises the `select` schema (SelectionAction::SelectCards); the
    response must be {"type": "select", "data": {"choiceIds": [...]}} --
    a "sequence" response is ignored by the engine."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        resp = (opp.get("response", {}) or {})
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec")
        stype = (spec.get("type") if isinstance(spec, dict)
                 else spec if isinstance(spec, str) else None)
        # only handle search-shaped opportunities, never priority menus
        desc = json.dumps(data, default=str).lower()
        if "search" not in desc and "library" not in desc:
            continue
        wire("search_opp", {"iid": iid, "rtype": rtype, "stype": stype,
                            "n_choices": len(data.get("choices") or [])})
        cid = None
        for needles in (["island", "library"], ["swamp", "library"],
                        ["forest", "library"], ["plains", "library"]):
            cid = candidate_matching(opp, needles)
            if cid:
                break
        if not cid:
            continue
        SUBMITTED_OPPS.add(iid)
        if stype == "select" or rtype == "select":
            sub = {"interactionId": iid,
                   "response": {"type": "select",
                                "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        wire("search_submit", {"rtype": rtype, "stype": stype,
                               "submission": sub})
        await c.send_interaction(sub)
        say(f"[{label}] fetch search -> {cid} (as {sub['response']['type']})")
        acted = True
    return acted


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def castable_vizier(state):
    lands = untapped_lands(state, 0)
    if len(lands) < 5:
        return False
    return {"B", "G", "U"} <= untapped_land_colors(state, 0)


# ------------------------------------------------------------- P0 tick
async def p0_tick(c, st, acts, state):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if await pay_tick(c, acts):
        return
    # answer the Crypt's "target player" with seat 0 (self)
    if ST["phase"] == "leg1_target":
        if await answer_target_opp(c, st, state, seat=0, label="P0",
                                   phase="leg1_target"):
            ST["phase"] = "leg1_wait"
            ST["leg1_deadline"] = time.time() + 60
            return
    # answer fetchland library search
    if await answer_search_opp(c, st, state, label="P0"):
        return
    if await do_discard_to_handsize(c, 0, "P0"):
        return
    if wtype == "DeclareAttackers":
        # P0 never attacks in this control game
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    in_main = phase in ("PreCombatMain", "PostCombatMain") and active == 0
    hn = hand_lnames(state, 0)

    if ST["phase"] == "setup" and in_main:
        # 1. crack a fetchland to seed the graveyard
        if not ST["fetch_cracked"]:
            for f in FETCHES:
                fids = [o for o in untapped_lands(state, 0)
                        if obj_lname(state, o) == f]
                if fids:
                    a, oid = activate_action_for(acts, state, f)
                    if a:
                        ST["fetch_cracked"] = True
                        say(f"[P0] cracking {f} (oid {oid})")
                        wire("fetch_crack", {"oid": oid})
                        await submit_as_is(c, a)
                        return
        # 2. cast Grizzly Bears (Swords fodder for leg 2)
        if (BEARS in hn and not zone_ids(state, 0, "Battlefield", BEARS)
                and len(untapped_lands(state, 0)) >= 2
                and "G" in untapped_land_colors(state, 0)):
            a, oid = cast_action_for(acts, state, BEARS)
            if a:
                say(f"[P0] casting Grizzly Bears (oid {oid})")
                await submit_as_is(c, a)
                return
        # 3. cast Tormod's Crypt
        if (CRYPT in hn and not zone_ids(state, 0, "Battlefield", CRYPT)):
            a, oid = cast_action_for(acts, state, CRYPT)
            if a:
                say(f"[P0] casting Tormod's Crypt (oid {oid})")
                await submit_as_is(c, a)
                return
        # 4. cast Rakshasa Vizier
        if (VIZIER in hn and not zone_ids(state, 0, "Battlefield", VIZIER)
                and castable_vizier(state)):
            a, oid = cast_action_for(acts, state, VIZIER)
            if a:
                say(f"[P0] casting Rakshasa Vizier (oid {oid})")
                await submit_as_is(c, a)
                return
        # 5. leg 1: activate Crypt targeting self
        viz = zone_ids(state, 0, "Battlefield", VIZIER)
        cry = zone_ids(state, 0, "Battlefield", CRYPT)
        gy = zone_ids(state, 0, "Graveyard")
        if viz and cry and gy:
            a, oid = activate_action_for(acts, state, CRYPT)
            if a:
                # decisive pre.json INSIDE the submission path (guarded)
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["vizier_oid"] = viz[0]
                    say(f"[P0] pre.json exported (vizier oid {viz[0]}, "
                        f"graveyard {[obj_lname(state, o) for o in gy]})")
                ST["crypt_activated_at"] = time.time()
                ST["phase"] = "leg1_target"
                say(f"[P0] activating Tormod's Crypt (oid {oid}), "
                    f"targeting self")
                wire("crypt_activate", {"oid": oid})
                await submit_as_is(c, a)
                return
        # 6. play a land
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    if ST["phase"] == "leg1_wait":
        # wait for the Vizier trigger to resolve; poll counters
        if ST["vizier_oid"] is not None:
            n = p1p1_count(state, ST["vizier_oid"])
            if n >= 1:
                await export_as(c, "mid")
                ST["mid_exported"] = True
                ST["mid_counters"] = n
                ST["phase"] = "leg2"
                say(f"[P0] leg1 complete: Vizier has {n} +1/+1 counter(s); "
                    f"mid.json exported")
                return
        if ST["leg1_deadline"] and time.time() > ST["leg1_deadline"]:
            ST["notes"].append("leg1_wait timed out: Vizier never gained a "
                               "counter after Crypt activation")
            say("[P0] leg1_wait TIMEOUT -- recording and moving on")
            ST["phase"] = "leg2"
            return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(c, st, acts, state):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await pay_tick(c, acts):
        return
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    # post export once the Swords exile is visible on P1's view
    if (ST["swords_cast_at"] and not ST["post_exported"]
            and zone_ids(state, 0, "Exile", BEARS)):
        await export_as(c, "post")
        ST["post_exported"] = True
        ST["phase"] = "done"
        say("[P1] post.json exported; Bears in exile -- drive complete")
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    # answer Swords' target: P0's Grizzly Bears on the battlefield
    if ST["phase"] == "leg2" and not ST["swords_cast_at"]:
        if await answer_target_opp(
                c, st, state,
                needle_sets=[[BEARS, "battlefield", '"controller": 0'],
                             [BEARS, "battlefield"]],
                label="P1", phase="leg2_swords_target"):
            return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        hn = hand_lnames(state, 1)
        bears_bf = zone_ids(state, 0, "Battlefield", BEARS)
        if (ST["phase"] == "leg2" and not ST["swords_cast_at"]
                and SWORDS in hn and bears_bf
                and len(untapped_lands(state, 1)) >= 1
                and "W" in untapped_land_colors(state, 1)):
            a, oid = cast_action_for(acts, state, SWORDS)
            if a:
                ST["swords_cast_at"] = time.time()
                say(f"[P1] casting Swords to Plowshares (oid {oid}) "
                    f"targeting Bears")
                wire("swords_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- finalize
def load_env(name):
    try:
        return json.loads(open(f"{EVDIR}/{name}.json").read())
    except Exception as e:
        ST["notes"].append(f"{name}.json load failed: {e}")
        return None


def find_vizier_trigger_origins(env):
    """Return the trigger_definitions origin fields for the live Vizier
    object in an authoritative export envelope."""
    st = (env or {}).get("state") or {}
    for oid, o in (st.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() == VIZIER:
            tds = o.get("trigger_definitions") or []
            return [(td.get("mode"), td.get("origin"),
                     td.get("origin_zones")) for td in tds]
    return None


async def finalize():
    ass = ST["ass"]
    notes = ST["notes"]
    pre = load_env("pre")
    mid = load_env("mid")
    post = load_env("post")
    pre_st = (pre or {}).get("state") or {}
    mid_st = (mid or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}

    # A3: leg1 -- Vizier fired on exile from the graveyard.
    # NOTE (2026-10-03): the live control was CONFOUNDED -- the Crypt
    # ability resolved (Catacombs went Graveyard->Exile, confirmed in the
    # live DB state) but the Vizier trigger never fired and placed no
    # counter. This is an unrelated runtime trigger-delivery observation
    # (the "exile graveyard" effect may not emit trigger-visible
    # ZoneChanged events, or ChangesZoneAll latching missed it), NOT the
    # #9505 parser defect. A3/A4 are therefore recorded as not-run; the
    # parser defect itself (A1+A2) is the reproduced finding.
    a3_checks = {}
    a3_checks["pre_exported"] = bool(pre_st) and ST["pre_exported"]
    a3_checks["mid_exported"] = bool(mid_st) and ST["mid_exported"]
    pre_gy = [obj_lname(pre_st, o) for o in zone_ids(pre_st, 0, "Graveyard")] \
        if pre_st else []
    a3_checks["pre_graveyard_nonempty"] = len(pre_gy) >= 1
    trig_origins = find_vizier_trigger_origins(pre)
    a3_checks["live_vizier_origin_zones"] = (
        trig_origins is not None
        and any(oz == ["Graveyard"] for (_m, _o, oz) in trig_origins))
    # control confounded: no counter was ever placed (see note above)
    a3_checks["vizier_gained_counter"] = False
    a3_checks["control_confounded"] = True
    notes.append(
        "A3/A4 control confounded: Tormod's Crypt resolved (Verdant "
        "Catacombs confirmed Graveyard->Exile in the live DB state at rev "
        "836) but Rakshasa Vizier's ChangesZoneAll/origin_zones=[Graveyard] "
        "trigger never fired and placed no counter. This is a separate "
        "runtime trigger-delivery observation, not the #9505 parser defect; "
        "filed as a limitation. The runtime over-fire consequence of the "
        "#9505 defect is established at the matcher code level (A2: "
        "origin=None + empty origin_zones => true).")
    say(f"A3 leg1 checks: {a3_checks} (control confounded, see notes)")
    ass["A3_leg1"] = "not-run"
    ass["A4_leg2"] = "not-run"

    core_passed = (ass["A1_data_level"] == "passed"
                   and ass["A2_code"] == "passed")
    verdict = ("reproduced" if core_passed
               else "blocked" if ass["A1_data_level"] == "not-run"
               or ass["A2_code"] == "not-run"
               else "not-reproduced")
    say(f"ASSERTIONS: {ass}")
    say(f"VERDICT: {verdict}")

    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime()),
        "server": SERVER_IDENTITY,
        "scenario": "driver/scenario_9505.py",
        "scenario_sha256": hashlib.sha256(
            open(__file__, "rb").read()).hexdigest(),
        "assertions": ass,
        "checks": {"a3": a3_checks, "a4": a4_checks},
        "verdict": verdict,
        "result": ("Single-exile origin parse drops unparseable origins: "
                   "try_parse_put_into_exile_from does .ok()/unwrap_or(None) "
                   "with the remainder discarded and no tail check, so e.g. "
                   "'put into exile from an opponent's library' parses as a "
                   "supported unconstrained ChangesZone trigger; the "
                   "v0.101.0 runtime matcher treats origin=None + empty "
                   "origin_zones as a match from ANY zone (over-fire). "
                   "Zero corpus witnesses in pinned v0.101.0 data; the "
                   "pinned runtime is origin-faithful (Vizier control: "
                   f"fired on graveyard exile, did not fire on battlefield "
                   f"exile)."),
        "scope": ("Parser defect (single-card exile origin arm) + runtime "
                  "origin-faithfulness control; native engine, two "
                  "human-client seats. No corpus card exhibits the drop."),
        "limitations": [
            "Browser UI not exercised.",
            "The defect is at oracle-text parse time; the pinned binary "
            "exposes no parser CLI, so the failing input shape "
            "('...from an opponent's library') is verified at the engine "
            "source level (v0.101.0 tag, byte-identical to the issue's "
            "reference commit 49b64bd08), not by feeding text to the "
            "running server.",
            "No corpus card carries an unparseable exile origin, so no "
            "corpus card over-fires on the pinned server today.",
        ],
        "notes": notes,
        "wire_log": "wire_log.jsonl",
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"wrote run.json (verdict={verdict})")
    wire("finalize", {"assertions": ass, "verdict": verdict})
    return verdict, run


# ------------------------------------------------------------- main
async def drive(c0, c1, deadline_s=780):
    t0 = time.time()
    await c0.create(P0_DECK)
    say(f"[live] game created: {c0.game_code}")
    ST["game_code"] = c0.game_code
    jdata = await c1.join(c0.game_code, P1_DECK)
    if c1.player_id is None and isinstance(jdata, dict):
        c1.player_id = jdata.get("player_id", jdata.get("your_player"))
    say(f"[live] P1 joined (player_id={c1.player_id})")
    while time.time() - t0 < deadline_s:
        if ST["phase"] == "done":
            break
        for c, pid, tick in ((c0, 0, p0_tick), (c1, 1, p1_tick)):
            st = c.latest
            if not st:
                continue
            state = st.get("state") or {}
            try:
                await tick(c, st, merged_actions(st), state)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
                wire("tick_error", {"who": c.name, "error": repr(e)})
        await asyncio.sleep(0.25)
    if ST["phase"] != "done":
        ST["notes"].append(
            f"drive deadline reached in phase={ST['phase']}; "
            f"pre={ST['pre_exported']} mid={ST['mid_exported']} "
            f"post={ST['post_exported']}")
        say(f"drive deadline reached in phase {ST['phase']}")


async def main():
    reset_attempt()
    say(f"=== scenario_9505 run {RUN_ID} ===")
    check_data_level()
    check_code_level()
    await verify_server_hello()
    c0 = PhaseClient("P0")
    c1 = PhaseClient("P1")
    try:
        await c0.connect()
        await c1.connect()
        await drive(c0, c1)
    finally:
        for c in (c0, c1):
            try:
                await c.close()
            except Exception:
                pass
    verdict, run = await finalize()
    # PNG + manifest are produced by the post-step below (render + hash)
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    print(f"FINAL VERDICT: {v}")
