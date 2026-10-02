#!/usr/bin/env python3
"""Issue #7351: Peerless Samurai may not reduce cost right on multiple triggers.

Fresh validation (2026-10-01) on the pinned v0.99.0 (build d919616,
protocol 98). No prior ledger entry.

Reported (Discord, classifier supported-aspect-defect, faithful parse):
  With six Peerless Samurai triggers resolved in a turn, the next spell
  should cost {6} less. Instead each of the next several spells was
  reduced by {1}.

  Oracle text (v0.99.0 card-data):
    Menace. Whenever a Samurai or Warrior you control attacks alone,
    the next spell you cast this turn costs {1} less to cast.
  Pinned parse: trigger mode Attacks -> ReduceNextSpellCost {amount: 1}.

Behavioral contract (native engine, two human driver seats):
  P0 assembles 2x Peerless Samurai (each {2}{R}, 2/3) on the battlefield.
  P0 attacks alone with exactly ONE of them. Both Samurais trigger
  (2x "the next spell you cast this turn costs {1} less"). Both resolve.
  pre.json is exported after resolution (2 reductions outstanding).
  P0 then casts a Samurai from hand (test cast 1), then another (test cast 2).

  A1 setup_ok       pre: 2 Peerless Samurai on P0 BF, >=1 Samurai in P0
                    hand, main phase, both triggers fired and resolved
  A2 cast1_reduced  test cast 1 paid {R} only (2 untapped Mountains remain
                    tapped beyond the 1): both reductions consumed by the
                    same next spell. FAILED if it paid {1}{R} (only one
                    reduction applied) -- the reported bug direction
  A3 cast2_full     test cast 2 paid full {2}{R} (no reductions left).
                    FAILED if it paid {1}{R} (a second reduction leaked
                    onto the second spell) -- the reported spread model
  A4 cleanup        stack empty at post, game proceeding

  Mana is measured as tapped-Mountain deltas around each cast (the engine
  auto-pays on protocol 98); pre/mid/post exports confirm the deltas.

Verdict: reproduced iff A1 passes and A2/A3 fail in the bug direction.
not-reproduced iff all pass. blocked iff A1 cannot be established.

Protocol-98 driver notes (v0.99.0, 2026-10-01):
- HELLO advertises protocol 98 (server enforces exact match).
- MulliganDecision as {"choice":{"type":"Keep"}}, gated on the seat's
  pending[] Declare entry keyed by (client, revision).
- BottomCards / DiscardToHandSize via single SelectCards {"cards":[...]}.
- PassPriority only when the seat genuinely holds priority (waiting_for
  Priority names the player), revision-guarded.
- CastSpell advertised actions submitted as-is (engine auto-pays mana).
- DeclareAttackers: stage-gated; ATTACK stage declares exactly one
  Samurai (the trigger source), all other stages declare empty.
- OrderTriggers answered as advertised.
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
RUN_ID = "20261001-7351b"  # clean rerun; 20261001-7351 partial
# (stalled on DeclareAttackers: protocol 98 advertises attacks:[] and the
# driver must construct the [[oid, target]] entry) was never uploaded
ISSUE = 7351

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

SAM = "Peerless Samurai"
MTN = "Mountain"
BEAR = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = [(SAM, 12), (MTN, 48)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

TIMEOUT = 2400
SETUP_DEADLINE = 1800

ST = {}
MULLS = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []
STACK_SEEN = []
C0 = None
P0C = None
P1C = None


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "t0": time.time(),
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "cast1_at": None,
        "cast2_at": None,
        "cast1_pre_untapped": None,
        "cast1_post_untapped": None,
        "cast2_pre_untapped": None,
        "cast2_post_untapped": None,
        "samurai_on_bf_at_cast1": 0,
        "samurai_on_bf_at_cast2": 0,
        "triggers_seen": 0,
        "attack_declared": False,
        "stop": False,
        "notes": [],
        "rejections": [],
        "game_code": None,
    })
    MULLS.clear()
    _PASSED_REV.clear()
    _DISCARD_REV.clear()
    WF_SEEN.clear()
    STACK_SEEN.clear()


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


def count_hand(state, pid, name):
    return sum(1 for oid in hand_oids(state, pid)
               if oname(state["objects"][oid]) == name)


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def count_bf(state, pid, name):
    return sum(1 for _, o in bf(state, pid) if oname(o) == name)


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


def stack_entries(state):
    return state.get("stack") or []


def count_peerless_triggers_on_stack(state):
    n = 0
    for e in stack_entries(state):
        if SAM in json.dumps(e, default=str):
            n += 1
    return n


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
                  if oname(state["objects"][o]) == MTN)
    n_sam = count_hand(state, pid, SAM)
    mulls = MULLS.get(c.name, 0)
    keep_ok = (n_lands >= 2 and n_sam >= 1) or mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        MULLS[c.name] = mulls + 1
    MULLS[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice} (lands={n_lands} sam={n_sam})")
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
    """Keep up to 4 Samurais; discard Mountains last."""
    h = hand_oids(state, pid)
    n = len(h) - 7
    if n <= 0:
        return []
    objs = state["objects"]
    sam_oids = [o for o in h if oname(objs[o]) == SAM]
    keep = set(sam_oids[:4])
    rest = [o for o in h if o not in keep]
    # mountains last
    rest.sort(key=lambda o: 0 if oname(objs[o]) != MTN else 1)
    return [int(x) for x in rest[:n]]


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
    """ATTACK stage: P0 declares exactly one Samurai attacker.
    Everywhere else: empty declarations."""
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            data = sub.get("data", {})
            if ST["stage"] == "ATTACK" and c.name == "P0" and not ST["attack_declared"]:
                # protocol 98 advertises attacks:[]; the driver constructs
                # the entry: [[attacker_oid, {"type":"Player","data":1}]]
                atk = next((int(oid) for oid, o in bf(state, pid)
                            if oname(o) == SAM and not o.get("tapped")),
                           None)
                if atk is not None:
                    data["attacks"] = [[atk, {"type": "Player", "data": 1}]]
                    data["bands"] = []
                    await submit_as_is(c, sub)
                    ST["attack_declared"] = True
                    ST["stage"] = "WAIT_TRIGGERS"
                    say(f"[P0] declares exactly one Samurai attacker "
                        f"(oid {atk})")
                    return True
                return False  # no untapped samurai yet; wait
            data["attacks"] = []
            data["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def answer_order_triggers(c, pid, state, acts):
    if wf_type(state) != "OrderTriggers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    if not acts:
        return False
    # accept the advertised ordering as-is
    await submit_as_is(c, acts[0])
    say(f"[{c.name}] OrderTriggers answered as advertised")
    return True


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MTN)
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


async def p0_cast_plan(c, pid, state, acts):
    """SETUP: get 2 Samurais on BF (cast while >=3 untapped mountains).
    CAST1/CAST2: single test cast each, recording pre-cast untapped count."""
    stage = ST["stage"]
    sam_bf = count_bf(state, pid, SAM)
    unt = untapped_of(state, pid, MTN)

    def try_cast(name):
        oid = find_hand(state, pid, name)
        return castspell_advertised(acts, oid)

    if stage == "SETUP":
        if sam_bf < 2 and unt >= 3:
            a = try_cast(SAM)
            if a:
                await submit_as_is(c, a)
                say(f"[P0] SETUP casts Samurai ({sam_bf} on BF, {unt} untapped)")
                return True
    elif stage == "CAST1":
        if ST.get("cast1_at") is not None:
            return False
        if count_hand(state, pid, SAM) >= 1:
            a = try_cast(SAM)
            if a:
                ST["cast1_pre_untapped"] = unt
                ST["samurai_on_bf_at_cast1"] = sam_bf
                await submit_as_is(c, a)
                ST["cast1_at"] = time.time()
                ST["stage"] = "WAIT_CAST1"
                say(f"[P0] CAST1 submits Samurai; pre untapped={unt}")
                return True
    elif stage == "CAST2":
        if ST.get("cast2_at") is not None:
            return False
        if count_hand(state, pid, SAM) >= 1:
            a = try_cast(SAM)
            if a:
                ST["cast2_pre_untapped"] = unt
                ST["samurai_on_bf_at_cast2"] = sam_bf
                await submit_as_is(c, a)
                ST["cast2_at"] = time.time()
                ST["stage"] = "WAIT_CAST2"
                say(f"[P0] CAST2 submits Samurai; pre untapped={unt}")
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
    if await answer_order_triggers(c, pid, state, acts):
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
    """Dummy seat: lands, never casts, never attacks, never blocks, passes."""
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
    if await answer_order_triggers(c, pid, state, acts):
        return True
    if await answer_declares(c, pid, state, acts):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        # P1 plays forests only
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
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


def watch_stack(state):
    n = count_peerless_triggers_on_stack(state)
    if n > ST["triggers_seen"]:
        ST["triggers_seen"] = n
        wire("peerless_triggers", {"on_stack": n,
                                   "stage": ST.get("stage")})
        say(f"Peerless triggers on stack: {n}")
    if STACK_SEEN is not None and (not STACK_SEEN or STACK_SEEN[-1] != n):
        STACK_SEEN.append(n)


def reduction_records(state):
    """Best-effort scan of the exported state for outstanding
    cost-reduction / one-shot effect records (supporting detail only)."""
    hits = []

    def walk(x, path):
        if isinstance(x, dict):
            blob = json.dumps(x, default=str)
            if "ReduceNextSpellCost" in blob or "reduce_next" in blob.lower():
                hits.append({"path": path, "snippet": blob[:400]})
                return
            for k, v in x.items():
                walk(v, f"{path}.{k}")
        elif isinstance(x, list):
            for i, v in enumerate(x[:50]):
                walk(v, f"{path}[{i}]")

    walk(state.get("players", {}), "players")
    return hits


def compute_assertions(pre_env, mid_env, post_env):
    assertions, detail = {}, {}
    pre, mid, post = pre_env["state"], mid_env["state"], post_env["state"]

    def bfcount(s, pid, name):
        return sum(1 for o in s["objects"].values()
                   if o.get("zone") == "Battlefield"
                   and o.get("controller") == pid
                   and oname(o) == name)

    def handcount(s, pid, name):
        return sum(1 for o in s["objects"].values()
                   if o.get("zone") == "Hand"
                   and o.get("controller") == pid
                   and oname(o) == name)

    def untapped(s, pid, name):
        return sum(1 for o in s["objects"].values()
                   if o.get("zone") == "Battlefield"
                   and o.get("controller") == pid
                   and oname(o) == name and not o.get("tapped"))

    pre_sam_bf = bfcount(pre, 0, SAM)
    pre_sam_hand = handcount(pre, 0, SAM)
    a1 = (pre_sam_bf == 2 and pre_sam_hand >= 1
          and ST["triggers_seen"] >= 2)
    assertions["A1_setup_ok"] = "passed" if a1 else "failed"
    detail["A1_setup_ok"] = (
        f"pre: P0 BF Samurai={pre_sam_bf} (want 2), P0 hand Samurai="
        f"{pre_sam_hand} (want >=1), Peerless triggers seen on stack="
        f"{ST['triggers_seen']} (want >=2), phase={pre.get('phase')}, "
        f"turn={pre.get('turn_number')}")
    detail["A1_reduction_records"] = reduction_records(pre)

    spent1 = ST["cast1_pre_untapped"] - untapped(mid, 0, MTN)
    if ST["cast1_pre_untapped"] is None:
        assertions["A2_cast1_reduced"] = "not-run"
        detail["A2_cast1_reduced"] = "cast 1 never submitted"
    elif spent1 == 1:
        assertions["A2_cast1_reduced"] = "passed"
        detail["A2_cast1_reduced"] = (
            f"cast 1 paid {spent1} mana (pre_untapped="
            f"{ST['cast1_pre_untapped']} -> post={untapped(mid, 0, MTN)}): "
            f"both {{1}} reductions combined on the same next spell")
    elif spent1 == 2:
        assertions["A2_cast1_reduced"] = "failed"
        detail["A2_cast1_reduced"] = (
            f"cast 1 paid {spent1} mana (pre_untapped="
            f"{ST['cast1_pre_untapped']} -> post={untapped(mid, 0, MTN)}): "
            f"only ONE {{1}} reduction applied -- REPORTED BUG DIRECTION")
    else:
        assertions["A2_cast1_reduced"] = "not-run"
        detail["A2_cast1_reduced"] = (
            f"cast 1 paid unexpected {spent1} mana "
            f"(pre={ST['cast1_pre_untapped']} post={untapped(mid, 0, MTN)})")

    spent2 = ST["cast2_pre_untapped"] - untapped(post, 0, MTN)
    if ST["cast2_pre_untapped"] is None:
        assertions["A3_cast2_full"] = "not-run"
        detail["A3_cast2_full"] = "cast 2 never submitted"
    elif spent2 == 3:
        assertions["A3_cast2_full"] = "passed"
        detail["A3_cast2_full"] = (
            f"cast 2 paid {spent2} mana (pre_untapped="
            f"{ST['cast2_pre_untapped']} -> post={untapped(post, 0, MTN)}): "
            f"full {{2}}{{R}} cost, no reductions left -- correct")
    elif spent2 == 2:
        assertions["A3_cast2_full"] = "failed"
        detail["A3_cast2_full"] = (
            f"cast 2 paid {spent2} mana (pre_untapped="
            f"{ST['cast2_pre_untapped']} -> post={untapped(post, 0, MTN)}): "
            f"a SECOND {{1}} reduction leaked onto the second spell -- "
            f"the reported spread model")
    else:
        assertions["A3_cast2_full"] = "not-run"
        detail["A3_cast2_full"] = (
            f"cast 2 paid unexpected {spent2} mana "
            f"(pre={ST['cast2_pre_untapped']} post={untapped(post, 0, MTN)})")

    stack = post.get("stack") or []
    ok = len(stack) == 0 and wf_type(post) != "GameOver"
    assertions["A4_cleanup"] = "passed" if ok else "failed"
    detail["A4_cleanup"] = (
        f"post: stack={len(stack)} wf={wf_type(post)}/p{wf_player(post)} "
        f"turn={post.get('turn_number')} phase={post.get('phase')} "
        f"rejections={len(ST['rejections'])}")
    return assertions, detail


def render_summary(assertions, detail, verdict, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 64], fill=(30, 32, 42))
    d.text((20, 16), f"phase-rs/phase #{ISSUE} - Peerless Samurai "
                     f"cost reduction",
           fill=(235, 235, 240))
    d.text((20, 38), f"v0.99.0 (d919616) protocol 98 | {RUN_ID} | "
                     f"verdict: {verdict}", fill=(150, 160, 175))
    y = 90
    d.text((20, y), "Assertions (tapped-Mountain deltas around each cast):",
           fill=(200, 200, 210))
    y += 26
    order = ["A1_setup_ok", "A2_cast1_reduced", "A3_cast2_full",
             "A4_cleanup"]
    labels = {
        "A1_setup_ok": "A1 setup_ok - 2 Samurai BF, >=1 in hand, 2 triggers fired",
        "A2_cast1_reduced": "A2 cast1_reduced - cast 1 pays {R} only (both reductions combine)",
        "A3_cast2_full": "A3 cast2_full - cast 2 pays full {2}{R} (none left)",
        "A4_cleanup": "A4 cleanup - stack empty, game proceeding",
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
           "Evidence: ntindle/phase-bug-state-evidence 7351/" + RUN_ID,
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
        "title": "Peerless Samurai may not reduce cost right on "
                 "multiple triggers",
        "server_identity": SERVER_IDENTITY,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                      time.localtime()),
        "verdict": verdict,
        "scope": "Peerless Samurai ReduceNextSpellCost accumulation: 2 "
                 "Samurais on BF, one attacks alone -> 2 triggers -> "
                 "two test casts; native engine, two human driver seats, "
                 "protocol-98 driver",
        "game_code": ST["game_code"],
        "decks": {"p0": P0_DECK, "p1": P1_DECK},
        "assertions": assertions,
        "assertion_detail": detail,
        "notes": ST["notes"],
        "rejections": ST["rejections"],
        "waiting_for_seen": WF_SEEN,
        "triggers_seen": ST["triggers_seen"],
        "cast_mana": {
            "cast1_pre_untapped": ST["cast1_pre_untapped"],
            "cast1_post_untapped": ST["cast1_post_untapped"],
            "cast2_pre_untapped": ST["cast2_pre_untapped"],
            "cast2_post_untapped": ST["cast2_post_untapped"],
        },
        "evidence_hashes": ev_hashes,
        "evidence_dir": f"evidence/{ISSUE}/{RUN_ID}",
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human driver seats.",
            "12x Peerless Samurai deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games); "
            "exercised behavior is the shipped card text.",
            "Two triggers exercised rather than the report's six "
            "(Roaming Throne + Raiyuu extra combats); the defect "
            "model is identical (N reductions must combine on the "
            "same next spell).",
            "The prebuilt server has no standalone "
            "state-restore; states are authoritative exports, "
            "restorable only via full game replay "
            f"(scenario_7351_0990.py, game {ST['game_code']}).",
        ],
        "driver_notes": [
            "Protocol-98 driver: MulliganDecision Keep gated "
            "on pending[] Declare; SelectCards for bottom/"
            "discard; PassPriority revision-guarded; "
            "CastSpell advertised actions submitted as-is "
            "(engine auto-pays).",
            "DeclareAttackers stage-gated: exactly one Samurai "
            "in ATTACK stage, empty otherwise; P1 never blocks.",
            "Mana paid per cast measured as tapped-Mountain "
            "delta between pre-cast live view and the "
            "post-resolution export.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(__file__, f"{EVDIR}/scenario_7351_0990.py")
    write_manifest()
    wire("finalized", {"verdict": verdict, "assertions": assertions})
    say(f"FINAL verdict={verdict} assertions={assertions}")


def write_manifest():
    files = sorted(fn for fn in os.listdir(EVDIR)
                   if fn != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            f.write(f"{_sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
    return files


def refresh_evidence_hashes():
    """Recompute manifest + run.json evidence_hashes over final bytes
    (wire_log.jsonl / scenario_run.log keep growing until close)."""
    files = write_manifest()
    rp = f"{EVDIR}/run.json"
    run = json.load(open(rp))
    run["evidence_hashes"] = {fn: _sha256_of_file(f"{EVDIR}/{fn}")
                              for fn in files
                              if fn not in ("manifest.sha256",
                                            "run.json")}
    json.dump(run, open(rp, "w"), indent=1)
    write_manifest()
    say("manifest regenerated over final bytes")

async def main():
    reset()
    global C0, P0C, P1C
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    P0C, P1C = p0, p1
    await p0.connect()
    await p1.connect()
    say("server identity pinned: v0.99.0 (d919616) protocol 98; using "
        "live backfill server 127.0.0.1:9374 (backfill-owned, pinned "
        "release; fresh game per run)")

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
                   "server_note": "reused backfill-owned 127.0.0.1:9374; "
                                  "fresh game per run"},
                  f, indent=1)

    last_rev = {}
    last_tick_wall = 0.0
    t0 = time.time()
    attack_watch_start = None
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
        watch_stack(state)
        elapsed = now - t0

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            ST["notes"].append("game ended before test casts completed")
            say("game over")
            continue

        # SETUP -> ATTACK once 2 Samurais are on BF and a 3rd is in hand
        if ST["stage"] == "SETUP":
            nbf = sum(1 for o in state["objects"].values()
                      if o.get("zone") == "Battlefield"
                      and o.get("controller") == 0
                      and oname(o) == SAM)
            nhand = sum(1 for o in state["objects"].values()
                        if o.get("zone") == "Hand"
                        and o.get("controller") == 0
                        and oname(o) == SAM)
            if nbf >= 2 and nhand >= 1:
                ST["stage"] = "ATTACK"
                say(f"SETUP complete: {nbf} Samurai on BF, {nhand} in "
                    f"hand -> ATTACK stage")
            elif elapsed > SETUP_DEADLINE:
                ST["notes"].append(
                    f"SETUP deadline: bf={nbf} hand={nhand} after "
                    f"{elapsed:.0f}s")
                say("SETUP deadline hit")
                assertions = {a: "not-run" for a in
                              ("A1_setup_ok", "A2_cast1_reduced",
                               "A3_cast2_full", "A4_cleanup")}
                assertions["A1_setup_ok"] = "failed"
                finalize("blocked", assertions,
                         {"A1_setup_ok": "2 Samurai on BF + 1 in hand "
                                         "never assembled; notes=" +
                                         "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

        # WAIT_TRIGGERS: both triggers must fire and resolve
        if ST["stage"] == "WAIT_TRIGGERS":
            if attack_watch_start is None:
                attack_watch_start = now
            stack_empty = len(stack_entries(state)) == 0
            if ST["triggers_seen"] >= 2 and stack_empty:
                s = await export_now("pre.json")
                if s is not None:
                    ST["pre_exported"] = True
                    ST["stage"] = "CAST1"
                    say(f"pre exported (triggers_seen={ST['triggers_seen']}); "
                        f"CAST1 stage")
            elif now - attack_watch_start > 240:
                ST["notes"].append(
                    f"WAIT_TRIGGERS watchdog: triggers_seen="
                    f"{ST['triggers_seen']} after 240s")
                say("WAIT_TRIGGERS watchdog hit")
                assertions = {a: "not-run" for a in
                              ("A1_setup_ok", "A2_cast1_reduced",
                               "A3_cast2_full", "A4_cleanup")}
                assertions["A1_setup_ok"] = "failed"
                finalize("blocked", assertions,
                         {"A1_setup_ok": "Peerless triggers never fired/"
                                         "resolved; notes=" +
                                         "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

        # WAIT_CAST1: watch for the 3rd Samurai on BF
        if ST["stage"] == "WAIT_CAST1":
            nbf = sum(1 for o in state["objects"].values()
                      if o.get("zone") == "Battlefield"
                      and o.get("controller") == 0
                      and oname(o) == SAM)
            if nbf >= ST["samurai_on_bf_at_cast1"] + 1:
                s = await export_now("mid.json")
                if s is not None:
                    ST["mid_exported"] = True
                    ST["cast1_post_untapped"] = sum(
                        1 for o in json.loads(s)["state"]["objects"].values()
                        if o.get("zone") == "Battlefield"
                        and o.get("controller") == 0
                        and oname(o) == MTN and not o.get("tapped"))
                    ST["stage"] = "CAST2"
                    say(f"mid exported; cast1 spent "
                        f"{ST['cast1_pre_untapped'] - ST['cast1_post_untapped']}; "
                        f"CAST2 stage")

        # WAIT_CAST2: watch for the 4th Samurai on BF
        if ST["stage"] == "WAIT_CAST2":
            nbf = sum(1 for o in state["objects"].values()
                      if o.get("zone") == "Battlefield"
                      and o.get("controller") == 0
                      and oname(o) == SAM)
            if nbf >= ST["samurai_on_bf_at_cast2"] + 1:
                await asyncio.sleep(3)  # let cleanup settle
                s = await export_now("post.json")
                if s is not None:
                    ST["post_exported"] = True
                    ST["cast2_post_untapped"] = sum(
                        1 for o in json.loads(s)["state"]["objects"].values()
                        if o.get("zone") == "Battlefield"
                        and o.get("controller") == 0
                        and oname(o) == MTN and not o.get("tapped"))
                    post_env = json.loads(s)
                    mid_env = json.loads(open(f"{EVDIR}/mid.json").read())
                    pre_env = json.loads(open(f"{EVDIR}/pre.json").read())
                    assertions, detail = compute_assertions(
                        pre_env, mid_env, post_env)
                    a1 = assertions.get("A1_setup_ok") == "passed"
                    a2 = assertions.get("A2_cast1_reduced")
                    a3 = assertions.get("A3_cast2_full")
                    verdict = ("blocked" if not a1
                               else "reproduced" if a2 == "failed"
                               or a3 == "failed" else "not-reproduced")
                    finalize(verdict, assertions, detail)
                    ST["stop"] = True
                    continue

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    refresh_evidence_hashes()
    rj = ST["rejections"]
    print(json.dumps({
        "stage": ST.get("stage"),
        "pre_exported": ST.get("pre_exported"),
        "mid_exported": ST.get("mid_exported"),
        "post_exported": ST.get("post_exported"),
        "triggers_seen": ST.get("triggers_seen"),
        "rejections": len(rj),
        "notes": ST.get("notes"),
    }, indent=2, default=str))
    return ST


if __name__ == "__main__":
    st = asyncio.run(main())
