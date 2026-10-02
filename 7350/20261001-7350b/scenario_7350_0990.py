#!/usr/bin/env python3
"""Issue #7350: On Wings of Gold gives buff to non-Zombie, non-Token creatures.

Fresh validation (2026-10-01) on the pinned v0.99.0 (build d919616,
protocol 98). No prior ledger entry.

Reported (Discord, classifier supported-aspect-defect, fully_parsed):
  [[On Wings of Gold]] should only benefit zombies and token creatures,
  but it buffs non-Zombie, non-Token creatures too.

Oracle text (v0.99.0 card-data):
  "Creatures you control that are Zombies and/or tokens get +1/+1 and have flying.
   Whenever one or more cards leave your graveyard, create a 1/1 white Zombie
   creature token."
  mana_cost: {3}{W}, artifact.

Behavioral contract (native engine, two human driver seats):
  P0 assembles: On Wings of Gold, Shambling Ghast (Zombie, 1/1),
  Grizzly Bears (non-Zombie non-token, 2/2), and a Soldier token (1/1)
  from Raise the Alarm. Creatures are put on the battlefield BEFORE OWG
  so pre.json captures the unbuffed baseline; post.json is exported once
  OWG is on the battlefield.

  A1 setup_ok        post: OWG on P0 BF; Bear, Ghast, >=1 Soldier token on
                        P0 BF; no other buff sources on P0 BF
  A2 zombie_buffed   Ghast is 2/2 with flying (expected PASS: buff is legal)
  A3 token_buffed    Soldier token is 2/2 with flying (expected PASS)
  A4 plain_excluded  Bear is still 2/2 WITHOUT flying (expected FAIL per
                        the report: this is the reported bug)
  A5 cleanup         stack empty at post, game proceeding, no rejections on
                        the OWG cast path

Verdict: reproduced iff A1 passes and A4 fails (the non-Zombie non-token
Bear incorrectly carries +1/+1 and/or flying). not-reproduced iff A1..A5
all pass. blocked iff A1 cannot be established.

Protocol-98 driver notes (v0.99.0, 2026-10-01):
- HELLO advertises protocol 98 (server enforces exact match).
- MulliganDecision as {"choice":{"type":"Keep"}}, gated on the seat's
  pending[] Declare entry keyed by (client, revision).
- BottomCards / DiscardToHandSize via single SelectCards {"cards":[...]}.
- PassPriority only when the seat genuinely holds priority (waiting_for
  Priority names the player), revision-guarded.
- CastSpell advertised actions submitted as-is (engine auto-pays mana).
- DeclareAttackers/DeclareBlockers submitted empty; P0 never attacks.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7350b"
ISSUE = 7350

SERVER_IDENTITY = {
    "validated_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}


def _sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# recompute against on-disk artifacts; never copy hashes blindly
for _f, _k in (
        ("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.99.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts",
      flush=True)

EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OWG = "On Wings of Gold"
GHAST = "Shambling Ghast"
BEAR = "Grizzly Bears"
ALARM = "Raise the Alarm"
PLAINS, SWAMP, FOREST = "Plains", "Swamp", "Forest"

P0_DECK = [(OWG, 4), (GHAST, 8), (BEAR, 8), (ALARM, 4),
           (PLAINS, 20), (SWAMP, 8), (FOREST, 8)]
P1_DECK = [(BEAR, 4), (FOREST, 56)]

TIMEOUT = 2400
SETUP_DEADLINE = 1500  # creatures + OWG assembled, else blocked
CREATURE_DEADLINE = 1200  # creatures on BF (pre export), else blocked

ST = {}
MULLS = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []
C0 = None
P0C = None
P1C = None


def reset():
    ST.clear()
    ST.update({
        "stage": "CREATURES",
        "t0": time.time(),
        "pre_exported": False,
        "post_exported": False,
        "owg_cast_at": None,
        "stop": False,
        "notes": [],
        "rejections": [],
        "game_code": None,
    })
    MULLS.clear()
    _PASSED_REV.clear()
    _DISCARD_REV.clear()
    WF_SEEN.clear()


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
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("name") or o.get("card_name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_names(state, pid):
    return [oname(o) for _, o in bf(state, pid)]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def pending_for(state, pid):
    for p in wf_data(state).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    return (wf_type(state) == "Priority"
            and str(wf_player(state)) == str(pid))


def has_flying(o):
    blob = json.dumps([o.get("keywords"), o.get("base_keywords")]).lower()
    return "flying" in blob


def is_token_obj(o):
    ct = o.get("card_types") or {}
    core = ct.get("core_types") or []
    if "Token" in core:
        return True
    if o.get("is_token") is True:
        return True
    return False


def find_bf(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid, o
    return None, None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    """Export authoritative state to EVDIR/path. Never raises: on
    failure logs and returns None so the main loop survives and can
    retry."""
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def dump_bf_snapshot(state, label):
    rows = []
    for oid, o in bf(state, 0):
        rows.append({"oid": oid, "name": oname(o),
                     "power": o.get("power"), "toughness": o.get("toughness"),
                     "flying": has_flying(o), "token": is_token_obj(o),
                     "keywords": o.get("keywords"),
                     "base_keywords": o.get("base_keywords"),
                     "tapped": o.get("tapped")})
    wire(label, {"p0_battlefield": rows, "turn": state.get("turn_number"),
                 "phase": state.get("phase")})
    return rows


async def do_mulligan(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    if MULLS.get((c.name, c.revision)):
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    n_lands = sum(1 for o in hand_oids(state, pid)
                  if oname(state["objects"][o]) in (PLAINS, SWAMP, FOREST))
    has_owg = find_hand(state, pid, OWG) is not None
    mulls = MULLS.get(c.name, 0)
    keep_ok = (has_owg and n_lands >= 2) or mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        MULLS[c.name] = mulls + 1
    MULLS[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice}")
    return True


async def do_bottom(c, pid):
    st = c.latest
    if not st:
        return False
    if MULLS.get((c.name, "bottom", c.revision)):
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    ph = pend.get("phase") or {}
    if ph.get("type") != "BottomCards":
        return False
    n = int(ph.get("count", 1) or 1)
    picks = [int(x) for x in hand_oids(state, pid)[:n]]
    MULLS[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


def discard_picks(state, pid):
    h = hand_oids(state, pid)
    n = len(h) - 7
    if n <= 0:
        return []
    objs = state["objects"]
    # keep one of each spell; discard extra creatures first, then
    # extra spells, then lands last
    keep = set()
    for nm in (OWG, GHAST, BEAR, ALARM):
        oid = find_hand(state, pid, nm)
        if oid:
            keep.add(oid)
    pref = [o for o in h if o not in keep
            and oname(objs[o]) in (BEAR, GHAST)]
    pref += [o for o in h if o not in keep and o not in pref
             and oname(objs[o]) not in (PLAINS, SWAMP, FOREST)]
    pref += [o for o in h if o not in keep and o not in pref]
    return [int(x) for x in pref[:n]]


async def do_discard(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_type(state) != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    picks = discard_picks(state, pid)
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} discards {len(picks)}")
    return True


async def pass_priority(c, pid):
    st = c.latest
    if not st:
        return False
    if not my_priority(st["state"], pid):
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get((c.name, rev)):
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            _PASSED_REV[(c.name, rev)] = True
            await submit_as_is(c, a)
            return True
    return False


async def answer_declares(c, pid, state, acts):
    """Empty attack/block declarations so the game never waits on us."""
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            say(f"[{c.name}] declare empty attacks")
            return True
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say(f"[{c.name}] declare no blockers")
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    for land in (PLAINS, SWAMP, FOREST):
        lid = find_hand(state, pid, land)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    return False


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def creatures_present(state):
    """(bear_on_bf, ghast_on_bf, token_on_bf) for P0."""
    _, bear = find_bf(state, 0, BEAR)
    _, ghast = find_bf(state, 0, GHAST)
    token = any(is_token_obj(o) and oname(o) == "Soldier"
                for _, o in bf(state, 0))
    return bear is not None, ghast is not None, token


async def p0_cast_plan(c, pid, state, acts):
    """CREATURES stage: land, then Ghast -> Bear -> Raise the Alarm.
    OWG_CAST stage: cast OWG."""
    bear, ghast, token = creatures_present(state)
    plains = untapped_of(state, pid, PLAINS)
    swamp = untapped_of(state, pid, SWAMP)
    forest = untapped_of(state, pid, FOREST)
    total = plains + swamp + forest

    def try_cast(name):
        oid = find_hand(state, pid, name)
        a = castspell_advertised(acts, oid)
        return a

    if ST["stage"] == "CREATURES":
        if not ghast and swamp >= 1:
            a = try_cast(GHAST)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Shambling Ghast")
                return True
        if not bear and total >= 2 and forest >= 1:
            a = try_cast(BEAR)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Grizzly Bears")
                return True
        if not token and total >= 2 and plains >= 1:
            a = try_cast(ALARM)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Raise the Alarm")
                return True
    elif ST["stage"] == "OWG_CAST":
        # single cast only: a second OWG copy in hand must not be cast while
        # the first is resolving/on the battlefield (it pollutes the post
        # state and the A5 cleanup assertion)
        if ST.get("owg_cast_at") is not None:
            return False
        if total >= 4 and plains >= 1:
            a = try_cast(OWG)
            if a:
                await submit_as_is(c, a)
                ST["owg_cast_at"] = time.time()
                say("[P0] casts On Wings of Gold")
                return True
    return False


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"[P0] legend-choice submitted as-is: {a['type']}")
            return True
    if await answer_declares(c, pid, state, acts):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if await p0_cast_plan(c, pid, state, acts):
            return True
    if await pass_priority(c, pid):
        return True
    return False


async def p1_tick(c, pid):
    """Dummy seat: lands, never casts, never attacks, passes."""
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await answer_declares(c, pid, state, acts):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
    if await pass_priority(c, pid):
        return True
    return False


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "player": wf_player(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


def compute_assertions(pre_env, post_env, post_rows):
    """Return (assertions, detail) from the exported states."""
    assertions, detail = {}, {}
    post = post_env["state"]

    def row(name):
        for r in post_rows:
            if r["name"] == name:
                return r
        return None

    owg = row(OWG)
    bear = row(BEAR)
    ghast = row(GHAST)
    soldiers = [r for r in post_rows if r["name"] == "Soldier"]
    other_buff_sources = [r["name"] for r in post_rows
                          if r["name"] not in (OWG, BEAR, GHAST, "Soldier")
                          and "Land" not in str(r["name"])]

    a1 = (owg is not None and bear is not None and ghast is not None
          and len(soldiers) >= 1)
    assertions["A1_setup_ok"] = "passed" if a1 else "failed"
    detail["A1_setup_ok"] = (
        f"post: OWG={owg is not None} Bear={bear is not None} "
        f"Ghast={ghast is not None} SoldierTokens={len(soldiers)}; "
        f"P0 BF={[(r['name'], r['power'], r['toughness'], r['flying']) for r in post_rows]}")

    if ghast:
        ok = (ghast["power"] == 2 and ghast["toughness"] == 2
              and ghast["flying"])
        assertions["A2_zombie_buffed"] = "passed" if ok else "failed"
        detail["A2_zombie_buffed"] = (
            f"Ghast {ghast['power']}/{ghast['toughness']} "
            f"flying={ghast['flying']} (base 1/1, Zombie -> legal buff)")
    else:
        assertions["A2_zombie_buffed"] = "not-run"
        detail["A2_zombie_buffed"] = "no Ghast on P0 BF at post"

    if soldiers:
        s = soldiers[0]
        ok = (s["power"] == 2 and s["toughness"] == 2 and s["flying"])
        assertions["A3_token_buffed"] = "passed" if ok else "failed"
        detail["A3_token_buffed"] = (
            f"Soldier token {s['power']}/{s['toughness']} "
            f"flying={s['flying']} token={s['token']} (base 1/1 -> legal buff)")
    else:
        assertions["A3_token_buffed"] = "not-run"
        detail["A3_token_buffed"] = "no Soldier token on P0 BF at post"

    if bear:
        excluded = (bear["power"] == 2 and bear["toughness"] == 2
                    and not bear["flying"])
        assertions["A4_plain_excluded"] = "passed" if excluded else "failed"
        detail["A4_plain_excluded"] = (
            f"Bear {bear['power']}/{bear['toughness']} "
            f"flying={bear['flying']} (base 2/2, non-Zombie non-token -> "
            f"must stay 2/2 without flying; REPORTED OUTCOME: buffed)")
    else:
        assertions["A4_plain_excluded"] = "not-run"
        detail["A4_plain_excluded"] = "no Bear on P0 BF at post"

    stack = post.get("stack") or []
    owg_pending = any(OWG in json.dumps(e, default=str) for e in stack)
    rej = [r for r in ST["rejections"]
           if ST.get("owg_cast_at") and r["at"] >= ST["owg_cast_at"] - 1]
    ok = len(stack) == 0 and not owg_pending
    assertions["A5_cleanup"] = "passed" if ok else "failed"
    detail["A5_cleanup"] = (
        f"post: stack={len(stack)} OWG_pending={owg_pending} "
        f"rejections_on_owg_path={len(rej)} "
        f"wf={wf_type(post)}/p{wf_player(post)} turn={post.get('turn_number')} "
        f"phase={post.get('phase')}")
    return assertions, detail


def render_summary(assertions, detail, verdict, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 64], fill=(30, 32, 42))
    d.text((20, 16), f"phase-rs/phase #{ISSUE} - On Wings of Gold",
           fill=(235, 235, 240))
    d.text((20, 38), f"v0.99.0 (d919616) protocol 98 | {RUN_ID} | "
                     f"verdict: {verdict}", fill=(150, 160, 175))
    y = 90
    d.text((20, y), "Assertions (post.json, P0 battlefield):",
           fill=(200, 200, 210))
    y += 26
    order = ["A1_setup_ok", "A2_zombie_buffed", "A3_token_buffed",
             "A4_plain_excluded", "A5_cleanup"]
    labels = {
        "A1_setup_ok": "A1 setup_ok - OWG+Bear+Ghast+Soldier token on P0 BF",
        "A2_zombie_buffed": "A2 zombie_buffed - Ghast 2/2 flying (legal)",
        "A3_token_buffed": "A3 token_buffed - Soldier token 2/2 flying (legal)",
        "A4_plain_excluded": "A4 plain_excluded - Bear stays 2/2, no flying "
                             "(REPORTED BUG)",
        "A5_cleanup": "A5 cleanup - stack empty, game proceeding",
    }
    for a in order:
        v = assertions.get(a, "not-run")
        color = {"passed": (110, 220, 130), "failed": (240, 110, 110),
                 "not-run": (170, 170, 170)}[v]
        d.text((30, y), f"{labels[a]}", fill=(220, 220, 230))
        d.text((880, y), v, fill=color)
        y += 30
    y += 10
    d.text((20, y), "Detail:", fill=(200, 200, 210))
    y += 24
    for a in order:
        line = f"{a}: {detail.get(a, '')}"
        while len(line) > 118:
            d.text((30, y), line[:118], fill=(160, 165, 175))
            line = "    " + line[118:]
            y += 18
            if y > H - 40:
                break
        d.text((30, y), line[:118], fill=(160, 165, 175))
        y += 22
        if y > H - 40:
            break
    d.text((20, H - 24),
           "Evidence: ntindle/phase-bug-state-evidence 7350/" + RUN_ID,
           fill=(120, 125, 135))
    img.save(out_path)
    say(f"rendered {out_path}")


def finalize(verdict, assertions, detail):
    """Write assertions.json, summary.png, run.json, manifest and copy the
    scenario into EVDIR. Called exactly once at the end of the run."""
    import shutil
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": assertions, "detail": detail}, f, indent=1)
    render_summary(assertions, detail, verdict, f"{EVDIR}/summary.png")
    ev_hashes = {}
    for fn in sorted(os.listdir(EVDIR)):
        if fn in ("manifest.sha256", "assertions.json", "summary.png",
                  "run.json"):
            continue
        ev_hashes[fn] = _sha256_of_file(f"{EVDIR}/{fn}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "On Wings of Gold gives buff to non-Zombie, "
                 "non-Token creatures",
        "server_identity": SERVER_IDENTITY,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                      time.localtime()),
        "verdict": verdict,
        "scope": "On Wings of Gold static (+1/+1 & flying to Zombies/"
                 "tokens): P0 BF with Ghast (Zombie), Soldier token, "
                 "Grizzly Bears (plain); native engine, two human driver "
                 "seats, protocol-98 driver",
        "game_code": ST["game_code"],
        "decks": {"p0": P0_DECK, "p1": P1_DECK},
        "assertions": assertions,
        "assertion_detail": detail,
        "notes": ST["notes"],
        "rejections": ST["rejections"],
        "waiting_for_seen": WF_SEEN,
        "evidence_hashes": ev_hashes,
        "evidence_dir": f"evidence/{ISSUE}/{RUN_ID}",
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human driver seats.",
            "Dense playsets are a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone "
            "state-restore; states are authoritative exports, "
            "restorable only via full game replay "
            f"(scenario_7350_0990.py, game {ST['game_code']}).",
        ],
        "driver_notes": [
            "Protocol-98 driver: MulliganDecision Keep gated "
            "on pending[] Declare; SelectCards for bottom/"
            "discard; PassPriority revision-guarded; "
            "CastSpell advertised actions submitted as-is "
            "(engine auto-pays).",
            "P0 never attacks; both seats submit empty "
            "DeclareAttackers/DeclareBlockers.",
            "Creatures assembled before OWG so pre.json is a "
            "true unbuffed baseline.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(__file__, f"{EVDIR}/scenario_7350_0990.py")
    files = sorted(fn for fn in os.listdir(EVDIR)
                   if fn != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            f.write(f"{_sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
    wire("finalized", {"verdict": verdict, "assertions": assertions,
                       "files": files})
    say(f"FINAL verdict={verdict} assertions={assertions}")


async def main():
    reset()
    global C0, P0C, P1C
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    P0C, P1C = p0, p1
    await p0.connect()
    await p1.connect()
    say("server identity pinned: v0.99.0 (d919616) protocol 98, mode Full; "
        "using live backfill server 127.0.0.1:9374 (runs/20261001-7349; "
        "backfill-owned, pinned release, per-run game isolation via fresh "
        "game code)")

    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    C0 = p0
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")
    assert p0.player_id == 0 and p1.player_id == 1, "seat mismatch"
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_identity": SERVER_IDENTITY})
    with open(f"{EVDIR}/setup.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "game_code": p0.game_code,
                   "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                   "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                   "server_identity": SERVER_IDENTITY,
                   "server_note": "reused backfill-owned 127.0.0.1:9374 "
                                  "(runs/20261001-7349); fresh game per run"},
                  f, indent=1)

    last_rev = {}
    last_tick_wall = 0.0
    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                               (p1, p1.player_id, p1_tick)):
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(
                    {"at": now, "who": c.name, "type": r["type"],
                     "data": r["data"]} for r in rej)
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                                   (p1, p1.player_id, p1_tick)):
                try:
                    await tickfn(c, pid)
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
                last_rev[c.name] = c.revision
        else:
            for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                                   (p1, p1.player_id, p1_tick)):
                if c.revision != last_rev.get(c.name, -1):
                    try:
                        await tickfn(c, pid)
                    except Exception as e:
                        say(f"tick error {c.name}: {e}")
                    last_rev[c.name] = c.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        elapsed = now - t0

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            ST["notes"].append("game ended before OWG window completed")
            say("game over")
            continue

        # CREATURES stage -> export pre once all three are on the BF
        if ST["stage"] == "CREATURES":
            bear, ghast, token = creatures_present(state)
            if bear and ghast and token and not ST["pre_exported"]:
                s = await export_now("pre.json")
                if s is not None:
                    ST["pre_exported"] = True
                    ST["stage"] = "OWG_CAST"
                    rows = dump_bf_snapshot(json.loads(s)["state"],
                                            "pre_baseline")
                    say("pre exported; OWG_CAST stage. baseline: "
                        f"{[(r['name'], r['power'], r['toughness'], r['flying']) for r in rows]}")
            elif elapsed > CREATURE_DEADLINE:
                ST["notes"].append(
                    f"CREATURES deadline: bear={bear} ghast={ghast} "
                    f"token={token} after {elapsed:.0f}s")
                say("CREATURES deadline hit")
                assertions = {a: "not-run" for a in
                              ("A1_setup_ok", "A2_zombie_buffed",
                               "A3_token_buffed", "A4_plain_excluded",
                               "A5_cleanup")}
                assertions["A1_setup_ok"] = "failed"
                finalize("blocked", assertions,
                         {"A1_setup_ok": "creatures never all assembled; "
                                         "notes=" + "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

        # OWG_CAST stage -> export post once OWG is on the BF
        if ST["stage"] == "OWG_CAST":
            _, owg = find_bf(state, 0, OWG)
            if owg is not None and not ST["post_exported"]:
                # let continuous effects settle one more revision
                await asyncio.sleep(3)
                s = await export_now("post.json")
                if s is not None:
                    ST["post_exported"] = True
                    post_env = json.loads(s)
                    rows = dump_bf_snapshot(post_env["state"], "post_buffed")
                    say("post exported: "
                        f"{[(r['name'], r['power'], r['toughness'], r['flying'], r['token']) for r in rows]}")
                    assertions, detail = compute_assertions(
                        json.loads(open(f"{EVDIR}/pre.json").read()),
                        post_env, rows)
                    a1 = assertions.get("A1_setup_ok") == "passed"
                    a4 = assertions.get("A4_plain_excluded")
                    verdict = ("blocked" if not a1
                               else "reproduced" if a4 == "failed"
                               else "not-reproduced")
                    finalize(verdict, assertions, detail)
                    ST["stop"] = True
                    continue
            elif elapsed > SETUP_DEADLINE:
                ST["notes"].append(
                    f"OWG_CAST deadline after {elapsed:.0f}s "
                    f"(owg_cast_at={ST['owg_cast_at']})")
                assertions = {a: "not-run" for a in
                              ("A1_setup_ok", "A2_zombie_buffed",
                               "A3_token_buffed", "A4_plain_excluded",
                               "A5_cleanup")}
                assertions["A1_setup_ok"] = "failed"
                finalize("blocked", assertions,
                         {"A1_setup_ok": "OWG never reached P0 BF; "
                                         "notes=" + "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    rj = ST["rejections"]
    print(json.dumps({
        "stage": ST.get("stage"),
        "pre_exported": ST.get("pre_exported"),
        "post_exported": ST.get("post_exported"),
        "rejections": len(rj),
        "notes": ST.get("notes"),
    }, indent=2, default=str))
    return ST


if __name__ == "__main__":
    st = asyncio.run(main())
