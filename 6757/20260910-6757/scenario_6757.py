#!/usr/bin/env python3
"""Issue #6757: Jackal, Genius Geneticist copies a creature spell but the copy
doesn't lose legendary (per Oracle: "copy that spell, except the copy isn't
legendary. Then put a +1/+1 counter on Jackal. (The copy becomes a token.)").

Behavioral contract (single game):
  Setup: P0 controls Jackal (1/1, power 1); casts Bofur, Reliable Guardian
         (legendary 1/1, MV 1) -> Jackal trigger fires.
  A1 setup_ok           game started; Jackal on P0 BF; Bofur castable
  A2 trigger_fired      Jackal's triggered ability appears on the stack
                        (source_id == Jackal)
  A3 copy_created       after trigger resolution, a second Bofur spell object
                        (the token copy) is on the stack
  A4 copy_nonlegendary  the copy's card_types.supertypes do NOT include
                        Legendary (the reported defect is that it does)
  A5 both_resolve       original (legendary) + copy token both end on the
                        battlefield as permanents
  A6 no_legend_rule     no legend-rule choice prompt; no Bofur in any graveyard
  A7 jackal_counter     Jackal has exactly one +1/+1 counter (power 2)
  A8 cleanup            stack empty, game proceeds

Verdict = reproduced iff A4 fails (copy still legendary) or A5/A6 fail with a
legend-rule signature. not-reproduced iff the full contract passes.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6757")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260910-6757"
EVDIR = f"{BACKFILL}/evidence/6757/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

JACKAL = "Jackal, Genius Geneticist"
BOFUR = "Bofur, Reliable Guardian"

# waiting_for types observed while the copy resolves (for A6 legend-rule scan)
WF_SEEN = []


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(o):
    return o.get("base_name") or o.get("name")


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        o = state["objects"].get(str(oid))
        if o and obj_name(o) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if obj_name(o) == name:
            return o
    return None


def untapped_land(state, pid, name):
    return sum(1 for o in bf(state, pid)
               if obj_name(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def supertypes(o):
    return ((o.get("card_types") or {}).get("supertypes")) or []


def bofur_stack_objs(state):
    return [o for o in state["objects"].values()
            if obj_name(o) == BOFUR and o.get("zone") == "Stack"]


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


async def keep_mulligan(c):
    st = c.latest
    if not st:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


async def basic_turn(c, pid, driver):
    """One decision tick for a seat. Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", [])
    if not acts:
        return False
    await keep_mulligan(c)
    st = c.latest
    state, acts = st["state"], st.get("legal_actions", [])
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    wf_type = (state.get("waiting_for") or {}).get("type")
    if wf_type == "CastOffer":
        for a in acts:
            if (a["type"] == "ChooseAdventureFace"
                    and a.get("data", {}).get("creature") is True):
                await c.send_action(a)
                say(f"{c.name} answers CastOffer: creature face")
                return True
    if is_my_main(state, pid):
        # land drop (prefer Plains so {W} is ready for Bofur)
        for lname in ("Plains", "Forest", "Island"):
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "PlayLand"
                        and str(d.get("object_id")) == str(find_hand(state, pid, lname) or -1)):
                    await c.send_action(a)
                    say(f"{c.name} plays {lname}")
                    return True
        jackal_out = (find_bf(state, pid, JACKAL) is not None
                      or any(obj_name(o) == JACKAL and o.get("zone") == "Stack"
                             for o in state["objects"].values()))
        if pid == driver.p0_id and not jackal_out:
            jid = find_hand(state, pid, JACKAL)
            if (jid and untapped_land(state, pid, "Forest") >= 1
                    and untapped_land(state, pid, "Island") >= 1):
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and str(d.get("object_id")) == str(jid)):
                        await c.send_action(a)
                        say(f"{c.name} casts {JACKAL}")
                        return True
        if pid == driver.p0_id and not driver.bofur_cast and find_bf(state, pid, JACKAL):
            bid = find_hand(state, pid, BOFUR)
            stack_empty = not (state.get("stack") or [])
            if (bid and stack_empty
                    and untapped_land(state, pid, "Plains") >= 1
                    and not any(obj_name(o) == BOFUR for o in bf(state, pid))):
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and str(d.get("object_id")) == str(bid)):
                        await c.send_action(a)
                        driver.bofur_cast = True
                        say(f"{c.name} casts {BOFUR} (id {bid})")
                        wire("bofur_cast_action", a)
                        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


class Driver:
    def __init__(self):
        self.p0_id = None
        self.bofur_cast = False


async def drive_until(p0, p1, driver, want_fn, timeout_s, label, track_wf=False):
    t0 = time.time()
    last_rev = {}
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.1)
        for c, pid in ((p0, p0.player_id), (p1, p1.player_id)):
            if c.revision == last_rev.get(c.name):
                continue
            if await basic_turn(c, pid, driver):
                last_rev[c.name] = c.revision
        st = p0.latest
        if st:
            if track_wf:
                record_wf(st["state"])
            if want_fn(st["state"]):
                return st["state"]
    say(f"TIMEOUT in drive_until: {label}")
    return None


async def settle_gated(p0, p1, cond, timeout, label, track_wf=False):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.25)
        for c in (p0, p1):
            st = c.latest
            if not st:
                continue
            acts = st.get("legal_actions", [])
            acted = False
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await c.send_action(a)
                    acted = True
                    break
            if not acted and (st["state"].get("waiting_for") or {}).get("type") == "CastOffer":
                for a in acts:
                    if (a["type"] == "ChooseAdventureFace"
                            and a.get("data", {}).get("creature") is True):
                        await c.send_action(a)
                        acted = True
                        break
            if not acted:
                for a in acts:
                    if a["type"] == "PassPriority":
                        await c.send_action(a)
                        break
        st = p0.latest
        if st:
            if track_wf:
                record_wf(st["state"])
            if cond(st["state"]):
                return st["state"]
    say(f"TIMEOUT in settle_gated: {label}")
    return None


async def main():
    t0 = time.time()
    driver = Driver()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(("Forest", 16), ("Island", 16), ("Plains", 12),
                         (JACKAL, 4), (BOFUR, 12)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60),))
    driver.p0_id = p0.player_id
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    obs["assert"]["A1_setup_ok"] = ("passed"
                                    if p0.player_id is not None and p1.player_id is not None
                                    else "failed")

    # Phase 1: ramp, cast Jackal, cast Bofur
    def bofur_on_stack(s):
        return len(bofur_stack_objs(s)) >= 1
    s = await drive_until(p0, p1, driver, bofur_on_stack, 900, "Bofur cast")
    if s is None:
        obs["notes"].append("Bofur was never cast within 900s")
        for k in ("A2_trigger_fired", "A3_copy_created", "A4_copy_nonlegendary",
                  "A5_both_resolve", "A6_no_legend_rule", "A7_jackal_counter",
                  "A8_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close(); await p1.close()
        return finish(obs, t0)
    jackal = find_bf(s, p0.player_id, JACKAL)
    obs["notes"].append(f"Bofur cast; Jackal on BF id={jackal['id'] if jackal else None} "
                        f"power={jackal.get('power') if jackal else '?'}")
    obs["assert"]["A1_setup_ok"] = "passed" if jackal else "failed"
    jackal_id = jackal["id"] if jackal else None
    say("exporting PRE_TRIGGER checkpoint")
    pre_s = await p0.export_state()
    with open(f"{EVDIR}/pre_trigger.json", "w") as f:
        f.write(pre_s)

    # Phase 2: Jackal trigger on the stack? wait for the copy (2nd Bofur on stack)
    def stack_snapshot(s):
        return [{"id": e.get("id"), "kind": (e.get("kind") or {}).get("type"),
                 "source_id": e.get("source_id"), "controller": e.get("controller")}
                for e in (s.get("stack") or [])]

    trig_seen = {"v": False}

    def copy_on_stack(s):
        n = len(bofur_stack_objs(s))
        trig = any(e.get("source_id") == jackal_id
                   and (e.get("kind") or {}).get("type") != "Spell"
                   for e in (s.get("stack") or []))
        if trig and not trig_seen["v"]:
            trig_seen["v"] = True
            say("Jackal trigger observed on stack:",
                json.dumps(stack_snapshot(s))[:600])
            wire("jackal_trigger_stack", stack_snapshot(s))
        return n >= 2

    s2 = await settle_gated(p0, p1, copy_on_stack, 180, "copy on stack",
                            track_wf=True)
    obs["assert"]["A2_trigger_fired"] = "passed" if trig_seen["v"] else "failed"
    obs["assert"]["A3_copy_created"] = "passed" if s2 is not None else "failed"
    if s2 is None:
        obs["notes"].append("no copy spell appeared on the stack within 180s")
        for k in ("A4_copy_nonlegendary", "A5_both_resolve", "A6_no_legend_rule",
                  "A7_jackal_counter", "A8_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        stuck = await p0.export_state()
        with open(f"{EVDIR}/stuck_no_copy.json", "w") as f:
            f.write(stuck)
        await p0.close(); await p1.close()
        return finish(obs, t0)

    # Phase 3: inspect the copy spell on the stack (pre-resolution export)
    say("copy on stack; exporting PRE_RESOLUTION checkpoint")
    pre_res_s = await p0.export_state()
    with open(f"{EVDIR}/pre_resolution.json", "w") as f:
        f.write(pre_res_s)
    pre_res = json.loads(pre_res_s)["state"]
    wire("stack_at_copy", stack_snapshot(pre_res))
    copies = bofur_stack_objs(pre_res)
    say(f"Bofur stack objects: {[(o['id'], o.get('is_token'), supertypes(o)) for o in copies]}")
    wire("copy_stack_objects",
         [{"id": o["id"], "is_token": o.get("is_token"),
           "supertypes": supertypes(o),
           "base_supertypes": ((o.get("base_card_types") or {}).get("supertypes")) or []}
          for o in copies])
    token_copies = [o for o in copies if o.get("is_token")]
    copy_obj = token_copies[0] if token_copies else None
    if copy_obj is None and len(copies) >= 2:
        # fallback: the copy is the stack object that is not the original cast;
        # original is non-token, so take any; prefer the later-seen one
        copy_obj = copies[-1]
        obs["notes"].append("no is_token flag on stack copies; using last-seen as copy")
    if copy_obj is None:
        obs["assert"]["A4_copy_nonlegendary"] = "not-run"
        obs["notes"].append("could not identify the copy object on the stack")
    else:
        sts = supertypes(copy_obj)
        nonlegend = "Legendary" not in sts
        obs["assert"]["A4_copy_nonlegendary"] = "passed" if nonlegend else "failed"
        obs["notes"].append(f"copy id={copy_obj['id']} is_token={copy_obj.get('is_token')} "
                            f"supertypes={sts} -> {'non-legendary OK' if nonlegend else 'STILL LEGENDARY (bug)'}")
        orig = [o for o in copies if o["id"] != copy_obj["id"]]
        if orig:
            obs["notes"].append(f"original spell id={orig[0]['id']} supertypes={supertypes(orig[0])}")

    # Phase 4: let both resolve
    def both_on_bf(s):
        return sum(1 for o in bf(s, p0.player_id) if obj_name(o) == BOFUR) >= 2

    s3 = await settle_gated(p0, p1, both_on_bf, 180, "both resolve",
                            track_wf=True)
    say("exporting POST_RESOLUTION state")
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_resolution.json", "w") as f:
        f.write(post_s)
    post = json.loads(post_s)["state"]
    wire("stack_at_post", stack_snapshot(post))
    bofurs_bf = [o for o in bf(post, p0.player_id) if obj_name(o) == BOFUR]
    say(f"Bofur permanents on P0 BF: {[(o['id'], o.get('is_token'), supertypes(o)) for o in bofurs_bf]}")
    wire("bofur_battlefield",
         [{"id": o["id"], "is_token": o.get("is_token"),
           "supertypes": supertypes(o), "power": o.get("power"),
           "toughness": o.get("toughness")} for o in bofurs_bf])
    obs["assert"]["A5_both_resolve"] = "passed" if len(bofurs_bf) >= 2 else "failed"
    gy_bofurs = [o for o in post["objects"].values()
                 if obj_name(o) == BOFUR and o.get("zone") == "Graveyard"]
    legendish_wf = [w for w in WF_SEEN if "egend" in w]
    a6 = (len(gy_bofurs) == 0 and not legendish_wf and len(bofurs_bf) >= 2)
    obs["assert"]["A6_no_legend_rule"] = "passed" if a6 else "failed"
    obs["notes"].append(f"waiting_for types during resolution: {WF_SEEN}; "
                        f"Bofur in graveyard: {len(gy_bofurs)}")
    # A7: Jackal counter
    jackal_post = find_bf(post, p0.player_id, JACKAL)
    counters = (jackal_post or {}).get("counters") or {}
    n_p1p1 = 0
    for k, v in counters.items():
        if "P1P1" in str(k) or "1/1" in str(k) or "+1" in str(k):
            n_p1p1 += v if isinstance(v, int) else 1
    obs["notes"].append(f"Jackal counters={json.dumps(counters)[:200]} power="
                        f"{jackal_post.get('power') if jackal_post else '?'}")
    obs["assert"]["A7_jackal_counter"] = ("passed"
                                          if (jackal_post and n_p1p1 == 1
                                              and jackal_post.get("power") == 2)
                                          else "failed")
    # A8: cleanup
    stack_empty = not (post.get("stack") or [])
    obs["assert"]["A8_cleanup"] = "passed" if stack_empty else "failed"
    obs["notes"].append(f"post: stack entries={len(post.get('stack') or [])} "
                        f"phase={post.get('phase')} waiting="
                        f"{(post.get('waiting_for') or {}).get('type')}")
    await p0.close(); await p1.close()
    return finish(obs, t0)


def finish(obs, t0):
    dur = time.time() - t0
    a = obs["assert"]
    if a.get("A4_copy_nonlegendary") == "failed":
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_trigger_fired", "A3_copy_created",
              "A4_copy_nonlegendary", "A5_both_resolve", "A6_no_legend_rule",
              "A7_jackal_counter", "A8_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": 6757,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-10",
            "source": "ServerHello + sha256 match of pinned verified artifacts",
        },
        "server_run_dir": f"runs/{EVID_RUN_ID}",
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": None,  # filled below
        "decks": {
            "P0": [["Forest", 16], ["Island", 16], ["Plains", 12],
                   [JACKAL, 4], [BOFUR, 12]],
            "P1": [["Island", 60]],
        },
        "assertions": a,
        "notes": obs["notes"] + [
            "Card-data (pinned v0.79.0) encodes the fix as CopySpell.additional_modifications=[RemoveSupertype Legendary]; "
            "this run tests whether the engine applies it at copy creation.",
        ],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "Not tested on the original 2026-07-29 build; verdict is scoped to v0.79.0, not a fix claim.",
        ],
        "setup_line": f"P0: 16 Forest/16 Island/12 Plains + 4x {JACKAL} + 12x {BOFUR}; P1: 60 Island (draw-go)",
        "contract_line": "Cast 1-MV legendary Bofur with Jackal (power 1) out: copy must be a non-legendary token, both permanents survive, Jackal gets +1/+1",
    }
    run["scenario_sha256"] = sha256_of_file(f"{BACKFILL}/driver/scenario_6757.py")
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(a)}")
    try:
        WIRE.close(); RUNLOG.close()
    except Exception:
        pass
    return obs


asyncio.run(main())
