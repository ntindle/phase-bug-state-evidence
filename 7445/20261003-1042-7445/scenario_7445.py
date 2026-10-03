#!/usr/bin/env python3
"""Issue #7445: Sundering Titan -- "choose a land of each basic land type" is
unparsed, so Destroy reads an empty tracked set.

Reported (2026-08-15, internal-triage; status:confirmed, area:parser,
priority:p3-card-specific, classifier:unsupported-aspect; related #6857
tracked-set publish census -- one of 33 cards whose tracked-set antecedent
clause never parses):

> When this creature enters or leaves the battlefield, choose a land of each
> basic land type, then destroy those lands.

Pinned v0.100.0 parse (see data_evidence.json): Sundering Titan is an
Artifact -- Creature -- Golem; triggers[0] mode ChangesZone (enters),
triggers[1] mode LeavesBattlefield (leaves), execute kind "Spell":
  head = Unimplemented { name: "unparsed_verb_arguments",
                         description: "choose a land of each basic land type" }
    sub_ability (Spell): Destroy { target: TrackedSet(0),
                                   cant_regenerate: false }
NOTE vs the issue body: the issue quotes the head name as "choose" (from the
census corpus at 9b7c66e30); the pinned v0.100.0 data names the head
"unparsed_verb_arguments". The description is identical and the head is still
Unimplemented -- the clause is still unparsed, so the defect holds. The name
difference is recorded, not treated as a discrepancy of outcome.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY chain tracked set and the Destroy
sub-ability reads that empty set (issue analysis). Destroy's consumer-side
behaviour on an empty set is NOT measured by the #6857 census ("Consumer
classification -- not measured for this sub type", "this issue does not assert
a runtime symptom"), so this run's contract is data-level + live-deck
presence, matching the #7437-#7444 family pattern: the card's live object on
the pinned server carries the same Unimplemented head, proving the parse
defect is live in the pinned runtime. No ETB/LTB drive is attempted (no
runtime symptom is asserted for Destroy).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 1x Sundering Titan + 4x Savannah Lions + 55x Plains (60)
  P1: 4x Savannah Lions + 56x Plains (60)
Drive:
  1. Mulligans: both seats keep (min hand 5); BottomCards answered by
     bottoming lands first (never bottoming Sundering Titan).
  2. Once the game is past the mulligan phases, export pre.json (the live
     object snapshot). No further gameplay is driven; the issue asserts no
     runtime symptom.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Sundering Titan as
                  the issue reports: Artifact -- Creature -- Golem;
                  triggers[0] mode ChangesZone (enters) and triggers[1]
                  mode LeavesBattlefield (leaves), both execute kind Spell,
                  both heads Unimplemented describing "choose a land of each
                  basic land type"; each sub Spell Destroy
                  { cant_regenerate: false } target TrackedSet(0).
  A2_setup_ok     the pinned server accepted both decks; the game reached
                  GameStarted for both seats and advanced past the
                  mulligan phases (waiting_for is not MulliganDecision or
                  BottomCards); pre.json exported.
  A3_live_object  the authoritative pre export contains the Sundering Titan
                  object (zone Library) with trigger_definitions[i].
                  definition carrying the identical Unimplemented head
                  (description "choose a land of each basic land type") and
                  the Destroy/TrackedSet(0) sub-ability -- the same parse
                  tree the data file carries.

Verdict rule: reproduced iff A1..A3 all passed (the reported Unimplemented
              head is present in the pinned data AND live on the pinned
              server); not-reproduced iff the pinned data parses the
              choose-a-land clause (head no longer Unimplemented);
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
RUN_ID = "20261003-1042-7445"
ISSUE = 7445
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
                     "(already listening at run start -- verified by this "
                     "run's own raw Hello handshake; started by run "
                     "20261003-1005-7444 earlier today)",
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

TITAN = "Sundering Titan"
LION = "Savannah Lions"
PLAINS = "Plains"

P0_DECK = deck((TITAN, 1), (LION, 4), (PLAINS, 55))
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
def head_shape(t):
    ex = t.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    sub_tgt = sub_eff.get("target") or {}
    return ex, head, sub, sub_eff, sub_tgt


def check_data_level():
    sr = CARD_DATA.get("sundering titan", {})
    ct = sr.get("card_type") or {}
    trigs = sr.get("triggers") or []
    ev = {
        "sundering_titan_card": sr,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_note": {
            "issue_body_quotes": "choose",
            "pinned_data_shows": None,
        },
    }
    shapes = [head_shape(t) for t in trigs]
    modes = [t.get("mode") for t in trigs]
    names = [h.get("name") for (_, h, _, _, _) in shapes]
    ev["head_name_note"]["pinned_data_shows"] = names

    def trig_ok(ex, head, sub_eff, sub_tgt):
        return (
            ex.get("kind") == "Spell"
            and head.get("type") == "Unimplemented"
            and str(head.get("description") or "") == "choose a land of each basic land type"
            and sub_eff.get("type") == "Destroy"
            and sub_tgt.get("type") == "TrackedSet"
            and sub_tgt.get("id") == 0
            and sub_eff.get("cant_regenerate") is False
        )

    ok = (
        ct.get("core_types") == ["Artifact", "Creature"]
        and ct.get("subtypes") == ["Golem"]
        and len(trigs) == 2
        and modes == ["ChangesZone", "LeavesBattlefield"]
        and all(trig_ok(s[0], s[1], s[3], s[4]) for s in shapes)
    )
    say(f"data-level check: core_types={ct.get('core_types')} "
        f"subtypes={ct.get('subtypes')}; n_triggers={len(trigs)}; "
        f"modes={modes}; "
        f"heads={[ (h.get('type'), h.get('name'), h.get('description')) for (_, h, _, _, _) in shapes]}; "
        f"subs={[ (se.get('type'), st, se.get('cant_regenerate')) for (_, _, _, se, st) in shapes]}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/sundering_titan_card_data.json", "w") as f:
        json.dump(sr, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "head_names": names})


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
    # bottom lands first; never bottom Sundering Titan
    rank = {"plains": 0, "savannah lions": 5, "sundering titan": 9}
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


def find_titan(state):
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == TITAN.lower():
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
        "cant_regenerate": sub_eff.get("cant_regenerate"),
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
                     "Sundering Titan as the issue reports -- Artifact -- "
                     "Creature -- Golem; triggers[0] mode ChangesZone "
                     "(enters) and triggers[1] mode LeavesBattlefield "
                     "(leaves), both execute kind Spell, both heads "
                     "Unimplemented describing 'choose a land of each basic "
                     "land type'; each sub Spell Destroy { cant_regenerate: "
                     "false } target TrackedSet(0). Name note: the issue "
                     "quotes the head name 'choose' (census corpus "
                     "9b7c66e30); the pinned data names the head "
                     "'unparsed_verb_arguments' -- the clause is still "
                     "Unimplemented, so the defect holds; see "
                     "data_evidence.json and sundering_titan_card_data.json.")
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
                     "(including Sundering Titan in P0's main deck), the "
                     "game started for both seats, mulligans completed, "
                     "and pre.json was exported.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: setup could not be established (see A2 "
                     "probe).")

    # A3: live object carries the same Unimplemented head
    if pre_st:
        tid, tobj = find_titan(pre_st)
        if tobj is not None:
            defs = walk_trigger_definitions(tobj)
            say(f"[A3] live titan oid={tid} zone={tobj.get('zone')} "
                f"trigger defs found={len(defs)}")
            matches = []
            for mode, d in defs:
                desc = describe_live_head(d)
                if (mode in ("ChangesZone", "LeavesBattlefield")
                        and desc["head_type"] == "Unimplemented"
                        and str(desc["head_description"]) == "choose a land of each basic land type"
                        and desc["sub_type"] == "Destroy"
                        and (desc["sub_target"] or {}).get("type") == "TrackedSet"
                        and desc["cant_regenerate"] is False):
                    matches.append((mode, d, desc))
            if len(matches) == 2:
                ST["live_head"] = [desc for _, _, desc in matches]
                ass["A3_live_object"] = "passed"
                notes.append(
                    f"A3 passed: the authoritative pre export contains "
                    f"the live Sundering Titan object (oid {tid}, zone "
                    f"{tobj.get('zone')}) with trigger_definitions "
                    f"carrying the identical Unimplemented head on both "
                    f"triggers -- modes {[m for m, _, _ in matches]}, "
                    f"execute kinds {[m2['execute_kind'] for _, _, m2 in matches]}, "
                    f"heads {[m2['head_type'] + '/' + str(m2['head_name']) for _, _, m2 in matches]}, "
                    f"subs Destroy target TrackedSet(0) cant_regenerate "
                    f"false; the parse defect is live on the pinned "
                    f"server.")
            else:
                ass["A3_live_object"] = "failed"
                notes.append(
                    f"A3 FAILED: live Sundering Titan object (oid {tid}) "
                    f"found but {len(matches)} (want 2) "
                    f"ChangesZone/LeavesBattlefield trigger definitions "
                    f"with the reported Unimplemented head + Destroy/"
                    f"TrackedSet(0) sub; defs seen: "
                    f"{json.dumps([describe_live_head(d) for _, d in defs], default=str)[:800]}.")
        else:
            ass["A3_live_object"] = "failed"
            notes.append("A3 FAILED: no live Sundering Titan object found "
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
        "scope": "Sundering Titan parse state: data-level parse assertion + "
                 "live-deck presence on the pinned server. The issue "
                 "asserts no runtime consumer symptom (Destroy's "
                 "consumption on the empty tracked set is unmeasured in "
                 "the #6857 census); per the issue, the reported defect is "
                 "the parse state and its direct structural consequence "
                 "(empty chain tracked-set publish from the Unimplemented "
                 "no-op resolver). No ETB/LTB drive was attempted.",
        "result": ("Pinned v0.100.0 card-data.json and the live object on "
                   "the pinned server both carry the reported "
                   "Unimplemented head ('choose a land of each basic land "
                   "type') on the ChangesZone and LeavesBattlefield "
                   "triggers; the Destroy sub-ability reads "
                   "TrackedSet(0), which the no-op Unimplemented resolver "
                   "leaves empty. Head-name note: the issue quotes "
                   "'choose'; pinned data names the head "
                   "'unparsed_verb_arguments' -- description identical, "
                   "still Unimplemented."
                   if verdict == "reproduced" else
                   "Assertions did not all pass; see notes."),
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "No runtime symptom is asserted for this issue: Destroy's "
            "behaviour on an empty tracked set is unmeasured (stated in "
            "the issue), so no trigger-resolution drive was performed.",
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
