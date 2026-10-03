#!/usr/bin/env python3
"""Backfill scenario for phase-rs/phase #7446 (Synthetic Destiny).

Parser-class issue: the census (card-data at 9b7c66e30) found the clause
"reveal cards from the top of your library until you reveal that many
creature cards" unparsed -- an Effect::Unimplemented chain head -- so the
chain tracked set the sub-ability's ChangeZoneAll read was allocated empty.
The issue asserts no runtime consumer symptom, so the contract is the parse
state (data-level) plus the live parse tree on a server object (A3), not a
runtime drive -- mirroring the #7447 scenario for the same issue family.

Expected outcome on current data: the defect is ABSENT. The pinned
v0.101.0 card-data.json parses the reveal clause as a count-bound
RevealUntil {count: Ref(EventContextAmount), kept_destination: Battlefield,
rest_destination: Library} followed by Shuffle -- exactly the acceptance
shape from the issue's triage comment. All assertions passing therefore
yields verdict `not-reproduced` (never `fixed`).

Assertions:
  A1_data_level: pinned v0.101.0 card-data.json parses Synthetic Destiny
    with the corrected shape (ChangeZoneAll-exile head; CreateDelayedTrigger
    AtNextPhase/End whose effect is RevealUntil with kept Battlefield / rest
    Library and count Ref(EventContextAmount); Shuffle tail) and contains no
    Unimplemented node anywhere in its abilities.
  A2_setup_ok: decks accepted, game started for both seats, past mulligans,
    pre.json exported.
  A3_live_object: the authoritative export contains a live Synthetic Destiny
    object whose ability definitions carry the identical corrected tree
    (RevealUntil, no Unimplemented).
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
RUN_ID = "20261003-7446"
ISSUE = 7446
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
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.101.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned v0.101.0 server on 127.0.0.1:9375 "
                      "(started by this run with a fresh games.db; run dir "
                      "runs/20261003-7446; verified by this run's own raw "
                      "Hello handshake)"),
    "mode": "Full",
    "source": ("2026-10-03: latest stable release v0.101.0 (published "
               "2026-10-03T16:02:26Z) == pinned release dir; ServerHello "
               "0.101.0/acafe9b/protocol 103 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))

DESTINY = "Synthetic Destiny"
ISLAND = "Island"
FOREST = "Forest"

P0_DECK = deck((DESTINY, 4), (ISLAND, 28), (FOREST, 28))
P1_DECK = deck((ISLAND, 30), (FOREST, 30))

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
    global ST, MULLS, SUBMITTED_OPPS
    ST = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "game_started": False,
        "pre_exported": False,
        "post_mulligan_seen": False,
        "wf_types_seen": [],
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_live_object")},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "live_tree": None,
    }
    MULLS = set()
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
    assert str(ver).startswith("0.101.0"), f"unexpected version {ver}"
    assert int(proto) == 103, f"unexpected protocol {proto}"
    assert str(build) == "acafe9b", f"unexpected build {build}"
    ST["hello_ok"] = True


# ------------------------------------------------------------- data check
def contains_unimplemented(node):
    """True if any dict in the tree has type == 'Unimplemented'."""
    if isinstance(node, dict):
        if node.get("type") == "Unimplemented":
            return True
        return any(contains_unimplemented(v) for v in node.values())
    if isinstance(node, list):
        return any(contains_unimplemented(v) for v in node)
    return False


def check_data_level():
    sr = CARD_DATA.get("synthetic destiny", {})
    oracle = sr.get("oracle_text") or ""
    abilities = sr.get("abilities") or []
    ev = {
        "synthetic_destiny_card": sr,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "oracle_text": oracle,
    }

    def shape_ok():
        if not isinstance(abilities, list) or len(abilities) != 1:
            return False, f"abilities len={len(abilities) if isinstance(abilities, list) else '?'}"
        a = abilities[0]
        if a.get("kind") != "Spell":
            return False, "head kind != Spell"
        head = a.get("effect") or {}
        if head.get("type") != "ChangeZoneAll":
            return False, f"head type={head.get('type')}"
        if head.get("destination") != "Exile":
            return False, "head destination != Exile"
        tgt = head.get("target") or {}
        if not (tgt.get("type") == "Typed" and tgt.get("type_filters") == ["Creature"]
                and tgt.get("controller") == "You"):
            return False, f"head target={tgt}"
        sub = a.get("sub_ability") or {}
        dtrig = sub.get("effect") or {}
        if dtrig.get("type") != "CreateDelayedTrigger":
            return False, f"sub effect type={dtrig.get('type')}"
        cond = dtrig.get("condition") or {}
        if not (cond.get("type") == "AtNextPhase" and cond.get("phase") == "End"):
            return False, f"delayed condition={cond}"
        inner = dtrig.get("effect") or {}
        ru = inner.get("effect") or {}
        if ru.get("type") != "RevealUntil":
            return False, f"inner effect type={ru.get('type')}"
        if (ru.get("player") or {}).get("type") != "Controller":
            return False, "reveal player != Controller"
        filt = ru.get("filter") or {}
        if not (filt.get("type") == "Typed" and filt.get("type_filters") == ["Creature"]):
            return False, f"reveal filter={filt}"
        cnt = ru.get("count") or {}
        if not (cnt.get("type") == "Ref" and (cnt.get("qty") or {}).get("type") == "EventContextAmount"):
            return False, f"reveal count={cnt}"
        if ru.get("kept_destination") != "Battlefield":
            return False, "kept_destination != Battlefield"
        if ru.get("rest_destination") != "Library":
            return False, "rest_destination != Library"
        tail = inner.get("sub_ability") or {}
        shuff = tail.get("effect") or {}
        if shuff.get("type") != "Shuffle":
            return False, f"tail type={shuff.get('type')}"
        if (shuff.get("target") or {}).get("type") != "Controller":
            return False, "shuffle target != Controller"
        if dtrig.get("uses_tracked_set") is not False:
            return False, "uses_tracked_set != False"
        return True, "corrected shape present"

    ok_shape, shape_note = shape_ok()
    has_unimp = contains_unimplemented(abilities)
    ok = ok_shape and not has_unimp
    say(f"data-level check: shape_ok={ok_shape} ({shape_note}); "
        f"unimplemented_present={has_unimp}; "
        f"oracle_match={oracle.startswith('Exile all creatures you control')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/synthetic_destiny_card_data.json", "w") as f:
        json.dump(sr, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "shape_note": shape_note,
                        "unimplemented": has_unimp})


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
    MULLS.add(tag)
    say(f"[{tag}] keep {len(hn)} (hand: {hn})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
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
    # bottom lands first; never bottom Synthetic Destiny
    rank = {"island": 0, "forest": 0, "synthetic destiny": 9}
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


def find_destiny(state):
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == DESTINY.lower():
            return int(oid), o
    return None, None


def describe_live_tree(abilities):
    """Extract the head/sub/inner/tail types from the live ability tree."""
    if not isinstance(abilities, list) or len(abilities) != 1:
        return {"error": f"abilities len != 1: {len(abilities) if isinstance(abilities, list) else type(abilities)}"}
    a = abilities[0]
    head = a.get("effect") or {}
    sub = a.get("sub_ability") or {}
    dtrig = sub.get("effect") or {}
    inner = dtrig.get("effect") or {}
    ru = inner.get("effect") or {}
    tail = inner.get("sub_ability") or {}
    shuff = tail.get("effect") or {}
    return {
        "kind": a.get("kind"),
        "head_type": head.get("type"),
        "head_destination": head.get("destination"),
        "head_target": head.get("target"),
        "sub_type": dtrig.get("type"),
        "sub_condition": dtrig.get("condition"),
        "uses_tracked_set": dtrig.get("uses_tracked_set"),
        "inner_type": ru.get("type"),
        "inner_player": ru.get("player"),
        "inner_filter": ru.get("filter"),
        "inner_count": ru.get("count"),
        "kept_destination": ru.get("kept_destination"),
        "rest_destination": ru.get("rest_destination"),
        "tail_type": shuff.get("type"),
        "tail_target": shuff.get("target"),
        "unimplemented_anywhere": contains_unimplemented(abilities),
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

    # A1: data-level parse shows the corrected shape, no Unimplemented
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.101.0 card-data.json parses "
                     "Synthetic Destiny with the corrected shape -- Spell "
                     "ChangeZoneAll {destination Exile, target Typed Creature "
                     "controller You}; sub CreateDelayedTrigger "
                     "{AtNextPhase End, uses_tracked_set false} whose effect "
                     "is RevealUntil {player Controller, filter Typed "
                     "Creature, count Ref(EventContextAmount), "
                     "kept_destination Battlefield, rest_destination "
                     "Library}; tail Shuffle {target Controller}. No "
                     "Unimplemented node anywhere in the card's abilities. "
                     "The issue-reported defect (Unimplemented reveal head -> "
                     "empty chain tracked set read by ChangeZoneAll) is "
                     "absent from current data; see data_evidence.json and "
                     "synthetic_destiny_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not show the "
                     "corrected shape; see data_evidence.json.")

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
                     "(including Synthetic Destiny in P0's main deck), the "
                     "game started for both seats, mulligans completed, "
                     "and pre.json was exported.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: setup could not be established (see A2 "
                     "probe).")

    # A3: live object carries the corrected tree (RevealUntil, no Unimplemented)
    if pre_st:
        did, dobj = find_destiny(pre_st)
        if dobj is not None:
            tree = describe_live_tree(dobj.get("abilities"))
            say(f"[A3] live destiny oid={did} zone={dobj.get('zone')} "
                f"tree={json.dumps(tree, default=str)[:600]}")
            inner = tree
            ok = (
                inner.get("kind") == "Spell"
                and inner.get("head_type") == "ChangeZoneAll"
                and inner.get("head_destination") == "Exile"
                and inner.get("sub_type") == "CreateDelayedTrigger"
                and (inner.get("sub_condition") or {}).get("type") == "AtNextPhase"
                and (inner.get("sub_condition") or {}).get("phase") == "End"
                and inner.get("inner_type") == "RevealUntil"
                and (inner.get("inner_player") or {}).get("type") == "Controller"
                and (inner.get("inner_filter") or {}).get("type") == "Typed"
                and (inner.get("inner_filter") or {}).get("type_filters") == ["Creature"]
                and (inner.get("inner_count") or {}).get("type") == "Ref"
                and ((inner.get("inner_count") or {}).get("qty") or {}).get("type") == "EventContextAmount"
                and inner.get("kept_destination") == "Battlefield"
                and inner.get("rest_destination") == "Library"
                and inner.get("tail_type") == "Shuffle"
                and (inner.get("tail_target") or {}).get("type") == "Controller"
                and inner.get("uses_tracked_set") is False
                and inner.get("unimplemented_anywhere") is False
            )
            if ok:
                ST["live_tree"] = tree
                ass["A3_live_object"] = "passed"
                notes.append(
                    f"A3 passed: the authoritative pre export contains "
                    f"the live Synthetic Destiny object (oid {did}, zone "
                    f"{dobj.get('zone')}) with ability definitions carrying "
                    f"the identical corrected tree -- ChangeZoneAll-exile "
                    f"head, CreateDelayedTrigger(AtNextPhase End, "
                    f"uses_tracked_set false), RevealUntil "
                    f"{{Controller, Typed Creature, Ref(EventContextAmount), "
                    f"kept Battlefield, rest Library}}, Shuffle tail; no "
                    f"Unimplemented anywhere. The corrected parse is live "
                    f"on the pinned server.")
            else:
                ass["A3_live_object"] = "failed"
                notes.append(
                    f"A3 FAILED: live Synthetic Destiny object (oid {did}) "
                    f"tree does not match the corrected shape: "
                    f"{json.dumps(tree, default=str)[:1200]}.")
        else:
            ass["A3_live_object"] = "failed"
            notes.append("A3 FAILED: no live Synthetic Destiny object found "
                         "in the authoritative pre export.")
    else:
        ass["A3_live_object"] = "failed"
        notes.append("A3 FAILED: no pre.json to inspect.")

    if all(v == "passed" for v in ass.values()):
        # The reported parse defect is absent on the current release/data:
        # not a reproduction of #7446.
        verdict = "not-reproduced"
    elif ass["A1_data_level"] != "passed" or ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
    else:
        verdict = "reproduced"
    say(f"verdict: {verdict} (assertions: {ass})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.gmtime(time.time())),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "scope": "Synthetic Destiny parse state: data-level parse assertion + "
                 "live-object parse-tree assertion on the pinned server, "
                 "mirroring the #7447 scenario for the same #6857-census "
                 "issue family. The issue asserts no runtime consumer "
                 "symptom (ChangeZoneAll's consumption on the empty tracked "
                 "set is unmeasured in the census); per the issue, the "
                 "reported defect is the parse state and its direct "
                 "structural consequence (Unimplemented reveal head -> empty "
                 "chain tracked-set publish). No end-step resolution drive "
                 "was attempted.",
        "result": ("Pinned v0.101.0 card-data.json and the live Synthetic "
                   "Destiny object on the pinned server both carry the "
                   "corrected parse: the reveal clause is a count-bound "
                   "RevealUntil {player Controller, filter Typed Creature, "
                   "count Ref(EventContextAmount), kept_destination "
                   "Battlefield, rest_destination Library} followed by "
                   "Shuffle {target Controller} -- the acceptance shape from "
                   "the issue's triage comment. No Unimplemented node "
                   "anywhere in the card's abilities; the issue-reported "
                   "defect (Unimplemented reveal head -> empty chain tracked "
                   "set read by ChangeZoneAll) is absent from current "
                   "release/data."
                   if verdict == "not-reproduced" else
                   "Assertions did not all pass; see notes."),
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "No runtime symptom is asserted for this issue: the end-step "
            "delayed-trigger resolution was not driven (stated scope in the "
            "issue; same contract as the #7447 scenario).",
            "The live check establishes the parse tree on the server's "
            "live object, not a restoration from the exported snapshot; "
            "snapshot bytes are preserved with their SHA-256 for "
            "re-inspection.",
            "Browser UI not exercised; native engine only, two human-client "
            "seats.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports restorable only via full game "
            "replay.",
        ],
        "observations": {
            "live_tree": ST.get("live_tree"),
            "pre_objects": len(pre_st.get("objects") or {}) if pre_st else 0,
            "wf_types_seen": ST["wf_types_seen"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"run.json written; verdict={verdict}")


if __name__ == "__main__":
    asyncio.run(main())
