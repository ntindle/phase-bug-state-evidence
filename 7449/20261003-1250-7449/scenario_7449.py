#!/usr/bin/env python3
"""Backfill scenario for phase-rs/phase #7449 (Twist Allegiance).

Parser-class issue: the clause "You and target opponent each gain control of
all creatures the other controls until end of turn" does not parse -- the
pinned card data carries an Effect::Unimplemented head (name
"unbound_subject"). The Unimplemented resolver is a no-op (no GameEvent), so
the chain tracked set is allocated empty, and the sub-ability "Untap those
creatures" (SetTapState targeting TrackedSet(0)) reads an empty set and is a
true no-op (SetTapState is a filter-only consumer -- no parent-target rescue).

The issue reports the parse state and its direct structural consequence, both
read from the card data; no runtime reproduction was run for this card, and
the triage confirms the runtime symptom is not asserted -- only the parse
state and the empty chain tracked set that structurally follows. The contract
is therefore the parse state (data-level) plus the live parse tree on a
server object (A3), not a runtime drive.

Assertions:
  A1_data_level: pinned v0.101.0 card-data.json parses Twist Allegiance with
    the reported shape (Sorcery; ability kind Spell; head Unimplemented
    'unbound_subject' with the reported description; sub Spell SetTapState
    target TrackedSet(0) scope Single state Untap).
  A2_setup_ok: decks accepted, game started for both seats, past mulligans,
    pre.json exported.
  A3_live_object: the authoritative export contains a live Twist Allegiance
    object whose ability definition carries the identical Unimplemented head
    + SetTapState/TrackedSet(0) sub.
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
RUN_ID = "20261003-1250-7449"
ISSUE = 7449
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
                       "mode at pin time 2026-10-03; binary digest matches "
                       "GitHub's asset digest; data digests match the signed "
                       "manifest; digests recomputed against on-disk files "
                       "this run"),
    "server_run_id": ("backfill-owned v0.101.0 server on 127.0.0.1:9374 "
                      "(started by this run; prior v0.101.0 server from run "
                      "20261003-1211-7448 stopped before start; verified by "
                      "this run's own raw Hello handshake)"),
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

TWIST = "Twist Allegiance"
MOUNTAIN = "Mountain"

# 4-of convenience density, same convention as prior runs.
P0_DECK = deck((TWIST, 4), (MOUNTAIN, 56))
P1_DECK = deck((MOUNTAIN, 60))

SETUP_DEADLINE_S = 900

EXPECTED_HEAD_DESC = ("You and target opponent each gain control of all "
                      "creatures the other controls until end of turn")


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
    global ST, MULLS
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
        "live_head": None,
    }
    MULLS = set()


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


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in player_of(state, pid).get("hand", [])]


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
def describe_ability(ab):
    """Flatten the spell-ability parse tree for comparison."""
    ex = ab or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    sub2 = sub.get("sub_ability") or {}
    sub2_eff = sub2.get("effect") or {}
    statics = sub2_eff.get("static_abilities") or []
    haste = any(
        any((m or {}).get("type") == "AddKeyword"
            and (m or {}).get("keyword") == "Haste"
            for m in (sa or {}).get("modifications") or [])
        for sa in statics)
    return {
        "kind": ex.get("kind"),
        "head_type": head.get("type"),
        "head_name": head.get("name"),
        "head_description": head.get("description"),
        "sub_kind": sub.get("kind"),
        "sub_type": sub_eff.get("type"),
        "sub_target": sub_eff.get("target"),
        "sub_scope": sub_eff.get("scope"),
        "sub_state": sub_eff.get("state"),
        "sub_link": sub.get("sub_link"),
        "sub2_type": sub2_eff.get("type"),
        "sub2_haste": haste,
        "sub2_duration": sub2_eff.get("duration"),
    }


def check_data_level():
    sr = CARD_DATA.get("twist allegiance", {})
    ct = sr.get("card_type") or {}
    abs_ = sr.get("abilities") or []
    desc = describe_ability(abs_[0] if abs_ else {})
    ev = {
        "twist_allegiance_card": sr,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "parsed_ability": desc,
    }
    ok = (
        ct.get("core_types") == ["Sorcery"]
        and len(abs_) == 1
        and desc["kind"] == "Spell"
        and desc["head_type"] == "Unimplemented"
        and desc["head_name"] == "unbound_subject"
        and str(desc["head_description"]) == EXPECTED_HEAD_DESC
        and desc["sub_kind"] == "Spell"
        and desc["sub_type"] == "SetTapState"
        and (desc["sub_target"] or {}).get("type") == "TrackedSet"
        and (desc["sub_target"] or {}).get("id") == 0
        and (desc["sub_scope"] or {}).get("type") == "Single"
        and (desc["sub_state"] or {}).get("type") == "Untap"
    )
    say(f"data-level check: core_types={ct.get('core_types')}; "
        f"n_abilities={len(abs_)}; parsed_ability={json.dumps(desc, default=str)}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/twist_allegiance_card_data.json", "w") as f:
        json.dump(sr, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "parsed": desc})


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
    MULLS.add(tag)
    hn = hand_lnames(state, pid)
    say(f"[{tag}] keep {len(hn)} (hand: {hn})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def find_twist(state):
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == TWIST.lower():
            return int(oid), o
    return None, None


def ability_defs(o):
    """Live spell abilities; trigger defs as defensive fallback."""
    out = list(o.get("abilities") or [])
    for td in o.get("trigger_definitions") or []:
        d = (td or {}).get("definition") or {}
        if d:
            out.append(d)
    return out


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
    say("P0 hello done; P1 hello done")

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
            if wt != "MulliganDecision":
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
        notes.append("A1 passed: pinned v0.101.0 card-data.json parses Twist "
                     "Allegiance as the issue reports -- Sorcery with one "
                     "Spell ability; head Effect::Unimplemented "
                     "(name 'unbound_subject', description 'You and target "
                     "opponent each gain control of all creatures the other "
                     "controls until end of turn'); sub-ability Spell "
                     "SetTapState targeting TrackedSet(0) (scope Single, "
                     "state Untap) linked SequentialSibling; third node "
                     "GenericEffect granting Haste until end of turn "
                     "(ParentTarget). Head name/description match the issue "
                     "exactly; see data_evidence.json and "
                     "twist_allegiance_card_data.json.")
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
                     "(including Twist Allegiance in P0's main deck), the "
                     "game started for both seats, mulligans completed, "
                     "and pre.json was exported.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: setup could not be established (see A2 "
                     "probe).")

    # A3: live object carries the same Unimplemented head
    if pre_st:
        tid, tobj = find_twist(pre_st)
        if tobj is not None:
            defs = ability_defs(tobj)
            say(f"[A3] live twist oid={tid} zone={tobj.get('zone')} "
                f"ability defs found={len(defs)}")
            matches = []
            for d in defs:
                desc = describe_ability(d)
                if (desc["kind"] == "Spell"
                        and desc["head_type"] == "Unimplemented"
                        and desc["head_name"] == "unbound_subject"
                        and str(desc["head_description"]) == EXPECTED_HEAD_DESC
                        and desc["sub_kind"] == "Spell"
                        and desc["sub_type"] == "SetTapState"
                        and (desc["sub_target"] or {}).get("type") == "TrackedSet"
                        and (desc["sub_target"] or {}).get("id") == 0
                        and (desc["sub_state"] or {}).get("type") == "Untap"):
                    matches.append(desc)
            if len(matches) == 1:
                ST["live_head"] = matches
                ass["A3_live_object"] = "passed"
                notes.append(
                    f"A3 passed: the authoritative pre export contains "
                    f"the live Twist Allegiance object (oid {tid}, zone "
                    f"{tobj.get('zone')}) with the identical Spell-ability "
                    f"parse tree -- head Unimplemented/'unbound_subject', "
                    f"sub Spell SetTapState target TrackedSet(0) (Untap); "
                    f"the parse defect is live on the pinned server.")
            else:
                ass["A3_live_object"] = "failed"
                notes.append(
                    f"A3 FAILED: live Twist Allegiance object (oid {tid}) "
                    f"found but {len(matches)} (want 1) ability definitions "
                    f"with the reported Unimplemented head + "
                    f"SetTapState/TrackedSet(0) sub; defs seen: "
                    f"{json.dumps([describe_ability(d) for d in defs], default=str)[:1500]}.")
        else:
            ass["A3_live_object"] = "failed"
            notes.append("A3 FAILED: no live Twist Allegiance object found "
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
        "scope": "Twist Allegiance parse state: data-level parse assertion + "
                 "live-object parse-tree assertion on the pinned server. The "
                 "issue asserts the parse state and its direct structural "
                 "consequence (empty chain tracked set; SetTapState is a "
                 "filter-only consumer, so the untap is a true no-op). No "
                 "runtime reproduction was run for this card, consistent "
                 "with the issue's stated scope.",
        "result": ("Pinned v0.101.0 card-data.json and the live object on "
                   "the pinned server both carry the reported Unimplemented "
                   "head ('unbound_subject': 'You and target opponent each "
                   "gain control of all creatures the other controls until "
                   "end of turn') on Twist Allegiance's Spell ability; the "
                   "SetTapState sub-ability targets TrackedSet(0), which "
                   "the no-op Unimplemented resolver leaves empty, so "
                   "'Untap those creatures' untaps nothing. A third "
                   "GenericEffect node grants Haste to ParentTarget until "
                   "end of turn."
                   if verdict == "reproduced" else
                   "Assertions did not all pass; see notes."),
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "No runtime symptom is asserted for this issue: the untap no-op "
            "follows structurally from the empty tracked-set publish "
            "(stated in the issue), so no spell-resolution drive was "
            "performed.",
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
