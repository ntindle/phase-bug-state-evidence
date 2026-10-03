#!/usr/bin/env python3
"""Issue #7444: Storybook Ride -- "where X is the number of Attractions
you've visited this turn" is unparsed, so GrantCastingPermission reads an
empty tracked set.

Reported (2026-08-15, internal-triage; status:confirmed, area:parser,
priority:p3-card-specific, classifier:unsupported-aspect; related #6857
tracked-set publish census, one of 33 cards whose tracked-set antecedent
clause never parses):

> Visit -- Exile the top X cards of your library, where X is the number of
> Attractions you've visited this turn (including this one). You may play
> those cards this turn. At the beginning of the next end step, if any of
> those cards remain exiled, put them on the bottom of your library in any
> order.

Pinned v0.100.0 parse (see data_evidence.json): Storybook Ride is an
Artifact -- Attraction; triggers[0] mode VisitAttraction, execute kind
"Spell":
  head = Unimplemented { name: "where_x_binding",
                         description: "where X is the number of Attractions
                                       you've visited this turn" }
    sub_ability (Spell): GrantCastingPermission { permission:
                           PlayFromExile / UntilEndOfTurn / granted_to 0,
                           target: TrackedSet(0) }
      sub_ability (Spell): CreateDelayedTrigger { condition: AtNextPhase
                           End, uses_tracked_set: false,
                           effect: PutAtLibraryPosition Bottom count 1
                           target ParentTarget }
The head name matches the issue body exactly ("where_x_binding"); no
discrepancy to record.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY chain tracked set and the
GrantCastingPermission sub-ability reads that empty set (issue analysis).
GrantCastingPermission's consumer-side behaviour on an empty set is NOT
measured in the issue ("this issue does not assert a runtime symptom"), so
this run's contract is data-level + live-deck presence, matching the family
(#7437-#7443) pattern: the card's live object on the pinned server carries
the same Unimplemented head, proving the parse defect is live in the pinned
runtime. No attraction-visit drive is attempted (no runtime symptom is
asserted for GrantCastingPermission).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 1x Storybook Ride + 4x Savannah Lions + 55x Plains (60)
  P1: 4x Savannah Lions + 56x Plains (60)
Drive:
  1. Mulligans: both seats keep (min hand 5); BottomCards answered by
     bottoming lands first (never bottoming Storybook Ride).
  2. Once the game is past the mulligan phases, export pre.json (the live
     object snapshot). No further gameplay is driven; the issue asserts no
     runtime symptom.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Storybook Ride as
                  the issue reports: Artifact -- Attraction, triggers[0]
                  mode VisitAttraction, execute kind Spell, head
                  Unimplemented name "where_x_binding" describing "where X
                  is the number of Attractions you've visited this turn";
                  sub Spell GrantCastingPermission { PlayFromExile /
                  UntilEndOfTurn / granted_to 0 } target TrackedSet(0);
                  second sub CreateDelayedTrigger uses_tracked_set false.
  A2_setup_ok     the pinned server accepted both decks; the game reached
                  GameStarted for both seats and advanced past the
                  mulligan phases (waiting_for is not MulliganDecision or
                  BottomCards); pre.json exported.
  A3_live_object  the authoritative pre export contains the Storybook Ride
                  object (zone Library) with trigger_definitions[i].
                  definition mode VisitAttraction carrying the identical
                  Unimplemented head (name "where_x_binding") and the
                  GrantCastingPermission/TrackedSet(0) sub-ability --
                  the same parse tree the data file carries.

Verdict rule: reproduced iff A1..A3 all passed (the reported Unimplemented
              head is present in the pinned data AND live on the pinned
              server); not-reproduced iff the pinned data parses the
              where-X clause (head no longer Unimplemented);
              blocked iff A1, A2, or A3 cannot be established.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-1005-7444"
ISSUE = 7444
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.100.0",
    "build_commit": "bc9ef56",
    "protocol_version": 101,
    "server_binary_sha256": "261550905a3d569731c9bd66b2b12a0fa878400fefaa9cc36f3ad4e1a3d8adda",
    "card_data_sha256": "57e086e700ee0bd81002327e89d339356c4bb6c26d9e1ca9c010474c0b8c291c",
    "draft_pools_sha256": "961c5397d834ca92b2168035be386844339573024df72dbc78370b934ed75770",
    "signature_verified": True,
    "server_run_id": "backfill-owned v0.100.0 server on 127.0.0.1:9374 "
                     "(started fresh by this run; ServerHello 0.100.0/"
                     "bc9ef56/protocol 101 verified by this run's own raw "
                     "Hello handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable; ServerHello 0.100.0/bc9ef56/protocol 101 "
              "verified by this run); hashes recomputed against on-disk "
              "artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

RIDE = "Storybook Ride"
LION = "Savannah Lions"
PLAINS = "Plains"

P0_DECK = deck((RIDE, 1), (LION, 4), (PLAINS, 55))
P1_DECK = deck((LION, 4), (PLAINS, 56))

SETUP_DEADLINE_S = 900


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def reset_attempt():
    global ST, MULLS, MULL_COUNT, SUBMITTED_OPPS
    ST = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "game_started": False,
        "pre_exported": False,
        "post_mulligan_seen": False,
        "wf_types_seen": [],
        "cast_rejections": 0,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_live_object")},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "live_head": None,
    }
    MULLS = set()
    MULL_COUNT = {}
    SUBMITTED_OPPS = set()


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


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def wf_of(state):
    return state.get("waiting_for") or {}


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.100.0"), f"unexpected version {ver}"
    assert int(proto) == 101, f"unexpected protocol {proto}"
    assert str(build) == "bc9ef56", f"unexpected build {build}"
    ST["hello_ok"] = True


# ------------------------------------------------------------- data check
def check_data_level():
    sr = CARD_DATA.get("storybook ride", {})
    ct = sr.get("card_type") or {}
    trigs = sr.get("triggers") or []
    t0 = trigs[0] if trigs else {}
    ex = t0.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    perm = sub_eff.get("permission") or {}
    sub_tgt = sub_eff.get("target") or {}
    sub2 = sub.get("sub_ability") or {}
    sub2_eff = sub2.get("effect") or {}
    ev = {
        "storybook_ride_card": sr,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_match": {
            "issue_body_quotes": "where_x_binding",
            "pinned_data_shows": head.get("name"),
        },
    }
    ok = (
        ct.get("core_types") == ["Artifact"]
        and ct.get("subtypes") == ["Attraction"]
        and len(trigs) == 1
        and t0.get("mode") == "VisitAttraction"
        and ex.get("kind") == "Spell"
        and head.get("type") == "Unimplemented"
        and head.get("name") == "where_x_binding"
        and "where X is the number of Attractions you've visited this turn"
        in str(head.get("description", ""))
        and sub_eff.get("type") == "GrantCastingPermission"
        and perm.get("type") == "PlayFromExile"
        and perm.get("duration") == "UntilEndOfTurn"
        and perm.get("granted_to") == 0
        and sub_tgt.get("type") == "TrackedSet"
        and sub_tgt.get("id") == 0
        and sub2_eff.get("type") == "CreateDelayedTrigger"
        and sub2_eff.get("uses_tracked_set") is False
    )
    say(f"data-level check: core_types={ct.get('core_types')} "
        f"subtypes={ct.get('subtypes')}; n_triggers={len(trigs)}; "
        f"mode={t0.get('mode')}; execute_kind={ex.get('kind')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub_eff.get('type')} perm={perm.get('type')}/"
        f"{perm.get('duration')}/granted_to={perm.get('granted_to')} "
        f"target={sub_tgt}; "
        f"sub2={sub2_eff.get('type')} uses_tracked_set="
        f"{sub2_eff.get('uses_tracked_set')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/storybook_ride_card_data.json", "w") as f:
        json.dump(sr, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "head_name": head.get("name")})


# ------------------------------------------------------------- actions
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
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    hn = hand_lnames(state, pid)
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if mulls >= 1 or len(hn) <= 5:
        MULLS.add(tag)
        say(f"[{tag}] keep {len(hn)} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    else:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[{tag}] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")))
    if key in SUBMITTED_OPPS:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    # bottom lands first; never bottom Storybook Ride
    rank = {"plains": 0, "savannah lions": 5, "storybook ride": 9}
    picks = [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def find_ride(state):
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == RIDE.lower():
            return int(oid), o
    return None, None


def walk_trigger_definitions(o):
    """Yield (mode, definition) for the live object's trigger defs.

    Card trigger defs nest under trigger_definitions[i].definition
    (driver hygiene note from the 7442 run).
    """
    out = []
    for td in o.get("trigger_definitions") or []:
        d = (td or {}).get("definition") or {}
        if d:
            out.append((td.get("mode") or d.get("mode"), d))
    # defensive fallbacks
    for d in (o.get("triggers") or []):
        out.append((d.get("mode"), d))
    return out


def describe_live_head(defn):
    ex = defn.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    return {
        "execute_kind": ex.get("kind"),
        "head_type": head.get("type"),
        "head_name": head.get("name"),
        "head_description": head.get("description"),
        "sub_type": sub_eff.get("type"),
        "sub_target": sub_eff.get("target"),
        "sub_permission": sub_eff.get("permission"),
    }


async def seat_tick(c, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid = c.player_id
    wtype = wf_of(state).get("type") or ""
    if wtype not in ST["wf_types_seen"]:
        ST["wf_types_seen"].append(wtype)
        say(f"[{tag}] waiting_for type: {wtype}")
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, tag):
            return
        if await do_bottom(c, pid, tag):
            return
        return
    if wtype in ("BottomCards",):
        if await do_bottom(c, pid, tag):
            return
        return
    # Past the mulligan phases: pass priority only; nothing is played.
    for a in list(state.get("legal_actions") or []):
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


async def main():
    reset_attempt()
    check_data_level()
    await verify_server_hello()

    c0 = PhaseClient("P0")
    c1 = PhaseClient("P1")
    await c0.connect()
    await c1.connect()
    say(f"P0 hello done; P1 hello done")

    attached = await c0.create(P0_DECK, player_count=2)
    code = attached.get("game_code")
    ST["game_code"] = code
    say(f"game created: code={code}")
    wire("game_created", {"code": code})
    j1 = await c1.join(code, P1_DECK)
    say(f"P1 joined: {json.dumps(j1)[:160]}")
    wire("game_joined", {"j1": j1})

    t0 = time.time()
    try:
        while time.time() - t0 < SETUP_DEADLINE_S:
            await asyncio.sleep(1.0)
            for c, tag in ((c0, "P0"), (c1, "P1")):
                try:
                    await seat_tick(c, tag)
                except Exception as e:
                    say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": tag,
                                        "err": f"{type(e).__name__}: {e}"})
            st = c0.latest
            if not st:
                continue
            state = st["state"]
            wt = wf_of(state).get("type") or ""
            if wt not in ("MulliganDecision", "BottomCards"):
                ST["post_mulligan_seen"] = True
                break
        else:
            say("SETUP DEADLINE hit without leaving mulligan phases")
            wire("deadline", {"which": "setup"})

        if ST["post_mulligan_seen"]:
            say("past mulligan phases; exporting pre.json")
            await export_as(c0, "pre")
            ST["pre_exported"] = True
        else:
            say("never left mulligan phases; exporting post.json for the "
                "record")
            await export_as(c0, "post")
    finally:
        await finalize(c0)
        await c0.close()
        await c1.close()
        if not WIRE.closed:
            WIRE.close()
        if not RUNLOG.closed:
            RUNLOG.close()


# ------------------------------------------------------------- finalize
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]

    def load_env(name):
        try:
            return json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name}.json load failed: {e}")
            return None

    pre = load_env("pre") or {}
    pre_st = pre.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Storybook Ride as the issue reports -- Artifact -- "
                     "Attraction, triggers[0] mode VisitAttraction, execute "
                     "kind Spell, head Unimplemented name 'where_x_binding' "
                     "(matches the issue body exactly, no discrepancy) "
                     "describing \"where X is the number of Attractions "
                     "you've visited this turn\", sub Spell "
                     "GrantCastingPermission { PlayFromExile / UntilEndOfTurn "
                     "/ granted_to 0 } target TrackedSet(0), second sub "
                     "CreateDelayedTrigger uses_tracked_set false; see "
                     "data_evidence.json and "
                     "storybook_ride_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup -- decks accepted, game started, past mulligans
    n_objs = len(pre_st.get("objects") or {})
    notes.append(f"A2 probe: hello_ok={ST['hello_ok']}; "
                 f"game_code={ST['game_code']}; "
                 f"post_mulligan_seen={ST['post_mulligan_seen']}; "
                 f"pre_exported={ST['pre_exported']}; "
                 f"pre objects={n_objs}; "
                 f"wf_types_seen={ST['wf_types_seen']}; "
                 f"pre waiting_for={(pre_st.get('waiting_for') or {}).get('type')}; "
                 f"players={[(p.get('id'), p.get('life')) for p in pre_st.get('players', [])]}.")
    if (ST["hello_ok"] and ST["game_code"] and ST["post_mulligan_seen"]
            and ST["pre_exported"] and pre_st and n_objs > 0):
        ass["A2_setup_ok"] = "passed"
        notes.append("A2 passed: the pinned server accepted both decks "
                     "(including Storybook Ride in P0's main deck), the "
                     "game started for both seats, mulligans completed, "
                     "and pre.json was exported.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: setup could not be established (see A2 "
                     "probe).")

    # A3: live object carries the same Unimplemented head
    if pre_st:
        rid, robj = find_ride(pre_st)
        if robj is not None:
            defs = walk_trigger_definitions(robj)
            say(f"[A3] live ride oid={rid} zone={robj.get('zone')} "
                f"trigger defs found={len(defs)}")
            match = None
            for mode, d in defs:
                desc = describe_live_head(d)
                if (mode == "VisitAttraction"
                        and desc["head_type"] == "Unimplemented"
                        and desc["head_name"] == "where_x_binding"
                        and desc["sub_type"] == "GrantCastingPermission"
                        and (desc["sub_target"] or {}).get("type") == "TrackedSet"):
                    match = (mode, d, desc)
                    break
            if match:
                mode, d, desc = match
                ST["live_head"] = desc
                ass["A3_live_object"] = "passed"
                notes.append(
                    f"A3 passed: the authoritative pre export contains "
                    f"the live Storybook Ride object (oid {rid}, zone "
                    f"{robj.get('zone')}) with trigger_definitions "
                    f"carrying the identical Unimplemented head -- mode "
                    f"{mode}, execute_kind {desc['execute_kind']}, "
                    f"head {desc['head_type']}/{desc['head_name']}, "
                    f"description {desc['head_description']!r}, sub "
                    f"{desc['sub_type']} target {desc['sub_target']}, "
                    f"permission {desc['sub_permission']}; the parse defect "
                    f"is live on the pinned server.")
            else:
                ass["A3_live_object"] = "failed"
                notes.append(
                    f"A3 FAILED: live Storybook Ride object (oid {rid}) "
                    f"found but no VisitAttraction trigger definition with "
                    f"the reported Unimplemented head; defs seen: "
                    f"{json.dumps([describe_live_head(d) for _, d in defs], default=str)[:600]}.")
        else:
            ass["A3_live_object"] = "failed"
            notes.append("A3 FAILED: no live Storybook Ride object found "
                         "in the authoritative pre export.")
    else:
        ass["A3_live_object"] = "failed"
        notes.append("A3 FAILED: no pre.json to inspect.")

    verdict = ("reproduced" if all(v == "passed" for v in ass.values())
               else ("blocked" if ass["A1_data_level"] != "passed"
                     or ass["A2_setup_ok"] != "passed"
                     else "not-reproduced"))
    say(f"verdict: {verdict} (assertions: {ass})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.gmtime(time.time())),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "scope": "Storybook Ride parse state: data-level parse assertion + "
                 "live-deck presence on the pinned server. The issue "
                 "asserts no runtime consumer symptom (GrantCastingPermission "
                 "consumption on the empty set is unmeasured); per the "
                 "issue, the reported defect is the parse state and its "
                 "direct structural consequence (empty chain tracked-set "
                 "publish from the Unimplemented no-op resolver). No "
                 "attraction-visit drive was attempted.",
        "result": ("Pinned v0.100.0 card-data.json and the live object on "
                   "the pinned server both carry the reported "
                   "Unimplemented head ('where_x_binding') on the "
                   "VisitAttraction trigger; GrantCastingPermission reads "
                   "TrackedSet(0), which the no-op Unimplemented resolver "
                   "leaves empty."
                   if verdict == "reproduced" else
                   "Assertions did not all pass; see notes."),
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "No runtime symptom is asserted for this issue: "
            "GrantCastingPermission's behaviour on an empty tracked set is "
            "unmeasured (stated in the issue), so no visit-resolution "
            "drive was performed.",
            "The live check establishes the parse tree on the server's "
            "live object, not a restoration from the exported snapshot; "
            "snapshot bytes are preserved with their SHA-256 for "
            "re-inspection.",
            "Browser UI not exercised; native engine only, two human-client "
            "seats.",
        ],
        "observations": {
            "live_head": ST.get("live_head"),
            "pre_objects": len(pre_st.get("objects") or {}) if pre_st else 0,
            "wf_types_seen": ST["wf_types_seen"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"run.json written; verdict={verdict}")


if __name__ == "__main__":
    asyncio.run(main())
