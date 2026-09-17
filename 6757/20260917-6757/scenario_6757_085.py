#!/usr/bin/env python3
"""Issue #6757 re-validation on pinned v0.85.0 (protocol 72).

Jackal, Genius Geneticist: "Whenever you cast a creature spell with mana
value equal to Jackal's power, copy that spell, except the copy isn't
legendary. Then put a +1/+1 counter on Jackal. (The copy becomes a token.)"

Reported: the copy remains legendary (CopySpell AST dropped the
`isn't legendary` modification).

Behavioral contract (single game), same as the 20260910-6757 run:
  Setup: P0: 16 Forest / 16 Island / 12 Plains + 4x Jackal + 12x Bofur
         (legendary 1/1, MV 1). P1: 60 Island, draw-go.
  A1 setup_ok           game started; Jackal on P0 BF before the Bofur cast
  A2 trigger_fired      Jackal's triggered ability appears on the stack
                        (source_id == Jackal's battlefield object id)
  A3 copy_created       after trigger resolution, a second Bofur spell object
                        (the token copy) is on the stack
  A4 copy_nonlegendary  the copy's card_types.supertypes do NOT include
                        Legendary (the reported defect is that it does)
  A5 both_resolve       original (legendary) + copy token both end on the
                        battlefield as permanents
  A6 no_legend_rule     no legend-rule choice prompt; no Bofur in any graveyard
  A7 jackal_counter     Jackal has exactly one +1/+1 counter (power 1->2)
  A8 cleanup            stack empty, game proceeds

Verdict = reproduced iff A4 fails (copy still legendary) or A5/A6 fail with a
legend-rule signature. not-reproduced iff the full contract passes.

Protocol-72 driver conventions (per AGENTS.md): CreateGameWithSettings +
JoinGameWithPassword + start_when_full, merged_actions (legal_actions +
legal_actions_by_object), per-seat tick with revision-change-or-5s re-tick,
default PassPriority gated on my_priority, mulligan Keep via advertised
action, CastOffer answered from the advertised ChooseAdventureFace action,
engine auto-taps mana for casts (PayMana answered as-is when advertised).
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260917-6757"
EVDIR = f"{BACKFILL}/evidence/6757/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

JACKAL = "Jackal, Genius Geneticist"
BOFUR = "Bofur, Reliable Guardian"

GAME_TIMEOUT = 900
PHASE_TIMEOUT = 240


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
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def lname(state, oid):
    o = state["objects"].get(str(oid)) or {}
    return o.get("base_name") or o.get("name")


def player_of(state, pid):
    return state["players"][pid]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_objs(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if lname(state, oid) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf_objs(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o
    return None


def untapped_land(state, pid, name):
    return sum(1 for o in bf_objs(state, pid)
               if (o.get("base_name") or o.get("name")) == name
               and not o.get("tapped"))


def supertypes(o):
    return ((o.get("card_types") or {}).get("supertypes")) or []


def bofur_stack_objs(state):
    return [o for o in state["objects"].values()
            if (o.get("base_name") or o.get("name")) == BOFUR
            and o.get("zone") == "Stack"]


def stack_snapshot(state):
    return [{"id": e.get("id"),
             "kind": (e.get("kind") or {}).get("type"),
             "source_id": e.get("source_id"),
             "controller": e.get("controller")}
            for e in (state.get("stack") or [])]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if a.get("data") not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def keep_mulligan(c):
    t0 = time.time()
    while time.time() - t0 < 60:
        await asyncio.sleep(0.25)
        st = c.latest
        if not st:
            continue
        for a in merged_actions(st):
            if a["type"] == "MulliganDecision":
                await submit_as_is(
                    c, {"type": "MulliganDecision",
                        "data": {"choice": {"type": "Keep"}}})
                say(f"{c.name} keeps opening hand")
                return True
    say(f"{c.name}: no mulligan decision seen in 60s")
    return False


class Ctx:
    def __init__(self):
        self.p0_id = None
        self.jackal_cast = False
        self.bofur_cast = False
        self.land_turns = {0: set(), 1: set()}
        self.wf_seen = []
        self.server_hello = None
        self.game_code = None
        self.rejections = []
        self.revision_stall = {}
        self.logged_castoffer = set()
        self.logged_wait = set()


def record_wf(ctx, state):
    wt = wf_type(state)
    if wt and (not ctx.wf_seen or ctx.wf_seen[-1] != wt):
        ctx.wf_seen.append(wt)
        wire("waiting_for", {"type": wt, "data": wf_of(state).get("data")})


def choose_adventure_creature(acts):
    """Return the advertised ChooseAdventureFace action selecting the
    creature face, or None if not clearly identifiable (never guess)."""
    for a in acts:
        if a["type"] != "ChooseAdventureFace":
            continue
        d = a.get("data", {}) or {}
        if d.get("creature") is True:
            return a
        face = d.get("face")
        if isinstance(face, str) and face.lower() == "creature":
            return a
        choice = d.get("choice")
        if isinstance(choice, str) and choice.lower() == "creature":
            return a
    return None


async def tick(c, pid, ctx):
    """One decision tick for a seat. Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    acts = merged_actions(st)
    if not acts:
        return False
    wtype = wf_type(state)
    my_priority = (wtype == "Priority"
                   and state.get("priority_player") == pid)

    # 1. mana payment prompts: answer as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            say(f"{c.name} answers {a['type']}")
            return True

    # 2. CastOffer (Bofur's adventure half): choose the creature face
    if wtype == "CastOffer" and wf_player(state) == pid:
        ca = choose_adventure_creature(acts)
        if ca:
            await submit_as_is(c, ca)
            say(f"{c.name} answers CastOffer: creature face")
            wire("castoffer_answer", ca.get("data"))
            return True
        key = (c.name, c.revision)
        if key not in ctx.logged_castoffer:
            ctx.logged_castoffer.add(key)
            say(f"{c.name} CastOffer with no identifiable creature action; "
                f"waiting_for data={json.dumps(wf_of(state).get('data'))[:300]}; "
                f"acts={[a['type'] for a in acts]}")
            wire("castoffer_unidentified",
                 {"who": c.name, "rev": c.revision,
                  "acts": [(a["type"], a.get("data")) for a in acts
                           if a["type"] == "ChooseAdventureFace"],
                  "data": wf_of(state).get("data")})
        return False  # never pass while a decision is pending for us

    # 3. other non-priority waits naming this seat: do not pass blindly
    if wtype not in (None, "Priority") and wf_player(state) == pid:
        key = (c.name, c.revision, wtype)
        if key not in ctx.logged_wait:
            ctx.logged_wait.add(key)
            say(f"{c.name} waiting_for={wtype} names seat {pid}; "
                f"acts={[a['type'] for a in acts]}; not passing")
            wire("unhandled_wait", {"who": c.name, "wtype": wtype,
                                    "acts": [a["type"] for a in acts],
                                    "data": wf_of(state).get("data")})
        return False

    # 4. P0 main-phase development (never while our own spell is in flight)
    in_flight = ctx.bofur_cast
    if (pid == ctx.p0_id and not in_flight and my_priority
            and state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
        jackal_out = (find_bf(state, pid, JACKAL) is not None
                      or any((o.get("base_name") or o.get("name")) == JACKAL
                          and o.get("zone") == "Stack"
                          for o in state["objects"].values()))
        # land drop (per-turn guard; prefer Plains so {W} is ready)
        turn = state.get("turn_number")
        if turn not in ctx.land_turns[pid]:
            for lname_ in ("Plains", "Forest", "Island"):
                pl = next((a for a in acts
                           if a["type"] == "PlayLand"
                           and lname(state, (a.get("data") or {}).get("object_id"))
                           == lname_), None)
                if pl:
                    await submit_as_is(c, pl)
                    ctx.land_turns[pid].add(turn)
                    say(f"{c.name} plays {lname_} (turn {turn})")
                    return True
        # cast Jackal ({G}{U})
        if (not jackal_out and not ctx.jackal_cast):
            jid = find_hand(state, pid, JACKAL)
            if (jid and untapped_land(state, pid, "Forest") >= 1
                    and untapped_land(state, pid, "Island") >= 1):
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(jid)), None)
                if cs:
                    await submit_as_is(c, cs)
                    ctx.jackal_cast = True
                    say(f"{c.name} casts {JACKAL} (hand oid {jid})")
                    wire("jackal_cast_action", cs.get("data"))
                    return True
        # cast Bofur ({W}) once Jackal is out and the stack is clear
        if (not ctx.bofur_cast and find_bf(state, pid, JACKAL)
                and not (state.get("stack") or [])):
            bid = find_hand(state, pid, BOFUR)
            if (bid and untapped_land(state, pid, "Plains") >= 1
                    and not find_bf(state, pid, BOFUR)):
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(bid)), None)
                if cs:
                    await submit_as_is(c, cs)
                    ctx.bofur_cast = True
                    say(f"{c.name} casts {BOFUR} (hand oid {bid})")
                    wire("bofur_cast_action", cs.get("data"))
                    return True

    # 5. P1 land drop (draw-go)
    if pid != ctx.p0_id:
        turn = state.get("turn_number")
        if (turn not in ctx.land_turns[pid]
                and state.get("active_player") == pid
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            pl = next((a for a in acts
                       if a["type"] == "PlayLand"
                       and lname(state, (a.get("data") or {}).get("object_id"))
                       == "Island"), None)
            if pl:
                await submit_as_is(c, pl)
                ctx.land_turns[pid].add(turn)
                return True

    # 6. default: pass priority only when it is actually ours
    if my_priority:
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return True
    return False


async def drive(p0, p1, ctx, want_fn, timeout_s, label, track_wf=False):
    """Drive both seats until want_fn(p0 state) is true or timeout."""
    t0 = time.time()
    last = {p0.name: (-1, 0.0), p1.name: (-1, 0.0)}
    warned = set()
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.25)
        for c, pid in ((p0, ctx.p0_id), (p1, 1 - ctx.p0_id)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            last_rev, last_tick = last[c.name]
            # stale-client watchdog: flag a pump that stops advancing
            if rev == last_rev and c.name not in warned \
                    and time.time() - last_tick > 45:
                warned.add(c.name)
                say(f"WATCHDOG: {c.name} revision {rev} unchanged for 45s")
                wire("watchdog_stall", {"who": c.name, "rev": rev})
            may_act = (rev != last_rev) or (time.time() - last_tick > 5)
            if may_act:
                try:
                    acted = await tick(c, pid, ctx)
                except Exception as e:
                    say(f"tick error for {c.name}: {e}")
                    acted = False
                if acted:
                    last[c.name] = (rev, time.time())
        st = p0.latest
        if st:
            state = st.get("state") or {}
            if track_wf:
                record_wf(ctx, state)
            if want_fn(state):
                return state
            if state.get("game_over") or state.get("winner") is not None:
                say("game ended during", label)
                return None
    say(f"TIMEOUT in drive: {label}")
    return None


async def export_state(c, path):
    s = await c.export_state()
    with open(path, "w") as f:
        f.write(s)
    return json.loads(s)["state"]


async def main():
    t0 = time.time()
    ctx = Ctx()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    # capture ServerHello from the pump inbox (first message)
    await p0.create(deck(("Forest", 16), ("Island", 16), ("Plains", 12),
                         (JACKAL, 4), (BOFUR, 12)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60),))
    ctx.p0_id = p0.player_id
    ctx.p1_id = p1.player_id
    ctx.game_code = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    obs["assert"]["A1_setup_ok"] = ("passed"
                                    if p0.player_id is not None
                                    and p1.player_id is not None
                                    else "failed")

    await keep_mulligan(p0)
    await keep_mulligan(p1)

    # Phase 1: develop, cast Jackal, cast Bofur (creature face)
    s = await drive(p0, p1, ctx,
                    lambda st: len(bofur_stack_objs(st)) >= 1,
                    GAME_TIMEOUT, "Bofur cast")
    if s is None:
        obs["notes"].append("Bofur was never cast within the game timeout")
        for k in ("A2_trigger_fired", "A3_copy_created", "A4_copy_nonlegendary",
                  "A5_both_resolve", "A6_no_legend_rule", "A7_jackal_counter",
                  "A8_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0, ctx)
    jackal = find_bf(s, ctx.p0_id, JACKAL)
    obs["notes"].append(
        f"Bofur cast; Jackal on BF id={jackal['id'] if jackal else None} "
        f"power={jackal.get('power') if jackal else '?'}")
    obs["assert"]["A1_setup_ok"] = "passed" if jackal else "failed"
    jackal_id = jackal["id"] if jackal else None
    say("exporting PRE_TRIGGER checkpoint")
    pre_trigger = await export_state(p0, f"{EVDIR}/pre_trigger.json")

    # Phase 2: Jackal trigger on the stack, then the copy (2nd Bofur spell)
    trig_seen = {"v": False}

    def copy_on_stack(st):
        n = len(bofur_stack_objs(st))
        trig = any(e.get("source_id") == jackal_id
                   and (e.get("kind") or {}).get("type") == "TriggeredAbility"
                   for e in (st.get("stack") or []))
        if trig and not trig_seen["v"]:
            trig_seen["v"] = True
            say("Jackal trigger observed on stack:",
                json.dumps(stack_snapshot(st))[:600])
            wire("jackal_trigger_stack", stack_snapshot(st))
        return n >= 2

    s2 = await drive(p0, p1, ctx, copy_on_stack, PHASE_TIMEOUT,
                     "copy on stack", track_wf=True)
    obs["assert"]["A2_trigger_fired"] = "passed" if trig_seen["v"] else "failed"
    obs["assert"]["A3_copy_created"] = "passed" if s2 is not None else "failed"
    if s2 is None:
        obs["notes"].append("no copy spell appeared on the stack in "
                            f"{PHASE_TIMEOUT}s")
        for k in ("A4_copy_nonlegendary", "A5_both_resolve", "A6_no_legend_rule",
                  "A7_jackal_counter", "A8_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await export_state(p0, f"{EVDIR}/stuck_no_copy.json")
        await p0.close()
        await p1.close()
        return finish(obs, t0, ctx)

    # Phase 3: inspect the copy spell on the stack (pre-resolution export)
    say("copy on stack; exporting PRE_RESOLUTION checkpoint")
    pre_res = await export_state(p0, f"{EVDIR}/pre_resolution.json")
    wire("stack_at_copy", stack_snapshot(pre_res))
    copies = bofur_stack_objs(pre_res)
    say("Bofur stack objects: "
        f"{[(o['id'], o.get('is_token'), supertypes(o)) for o in copies]}")
    wire("copy_stack_objects",
         [{"id": o["id"], "is_token": o.get("is_token"),
           "supertypes": supertypes(o),
           "base_supertypes":
               ((o.get("base_card_types") or {}).get("supertypes")) or []}
          for o in copies])
    token_copies = [o for o in copies if o.get("is_token")]
    copy_obj = token_copies[0] if token_copies else None
    if copy_obj is None and len(copies) >= 2:
        copy_obj = copies[-1]
        obs["notes"].append("no is_token flag on stack copies; "
                            "using last-seen as copy")
    if copy_obj is None:
        obs["assert"]["A4_copy_nonlegendary"] = "not-run"
        obs["notes"].append("could not identify the copy object on the stack")
    else:
        sts = supertypes(copy_obj)
        nonlegend = "Legendary" not in sts
        obs["assert"]["A4_copy_nonlegendary"] = \
            "passed" if nonlegend else "failed"
        obs["notes"].append(
            f"copy id={copy_obj['id']} is_token={copy_obj.get('is_token')} "
            f"supertypes={sts} -> "
            f"{'non-legendary OK' if nonlegend else 'STILL LEGENDARY (bug)'}")
        orig = [o for o in copies if o["id"] != copy_obj["id"]]
        if orig:
            obs["notes"].append(
                f"original spell id={orig[0]['id']} "
                f"supertypes={supertypes(orig[0])} "
                f"is_token={orig[0].get('is_token')}")

    # Phase 4: let both resolve
    def both_on_bf(st):
        return sum(1 for o in bf_objs(st, ctx.p0_id)
                   if (o.get("base_name") or o.get("name")) == BOFUR) >= 2

    s3 = await drive(p0, p1, ctx, both_on_bf, PHASE_TIMEOUT,
                     "both resolve", track_wf=True)
    say("exporting POST_RESOLUTION state")
    post = await export_state(p0, f"{EVDIR}/post_resolution.json")
    wire("stack_at_post", stack_snapshot(post))
    bofurs_bf = [o for o in bf_objs(post, ctx.p0_id)
                 if (o.get("base_name") or o.get("name")) == BOFUR]
    say("Bofur permanents on P0 BF: "
        f"{[(o['id'], o.get('is_token'), supertypes(o)) for o in bofurs_bf]}")
    wire("bofur_battlefield",
         [{"id": o["id"], "is_token": o.get("is_token"),
           "supertypes": supertypes(o), "power": o.get("power"),
           "toughness": o.get("toughness")} for o in bofurs_bf])
    obs["assert"]["A5_both_resolve"] = \
        "passed" if len(bofurs_bf) >= 2 else "failed"
    gy_bofurs = [o for o in post["objects"].values()
                 if (o.get("base_name") or o.get("name")) == BOFUR
                 and o.get("zone") == "Graveyard"]
    legendish_wf = [w for w in ctx.wf_seen if "egend" in w]
    a6 = (len(gy_bofurs) == 0 and not legendish_wf and len(bofurs_bf) >= 2)
    obs["assert"]["A6_no_legend_rule"] = "passed" if a6 else "failed"
    obs["notes"].append(f"waiting_for types during resolution: {ctx.wf_seen}; "
                        f"Bofur in graveyard: {len(gy_bofurs)}")

    # A7: Jackal counter
    jackal_post = find_bf(post, ctx.p0_id, JACKAL)
    counters = (jackal_post or {}).get("counters") or {}
    n_p1p1 = 0
    for k, v in counters.items():
        if "P1P1" in str(k).upper() or "+1" in str(k):
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
                        f"phase={post.get('phase')} turn={post.get('turn_number')} "
                        f"waiting={wf_type(post)}")
    await p0.close()
    await p1.close()
    return finish(obs, t0, ctx)


def finish(obs, t0, ctx):
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
    result = {
        "issue": 6757,
        "run_id": RUN_ID,
        "game_code": ctx.game_code,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "verdict": verdict,
        "assertions": a,
        "notes": obs["notes"],
        "wf_seen": ctx.wf_seen,
        "rejections": ctx.rejections,
        "decks": {
            "P0": [["Forest", 16], ["Island", 16], ["Plains", 12],
                   [JACKAL, 4], [BOFUR, 12]],
            "P1": [["Island", 60]],
        },
        "scenario_sha256": sha256_of_file(__file__),
    }
    with open(f"{EVDIR}/scenario_result.json", "w") as f:
        json.dump(result, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(a)}")
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    return result


asyncio.run(main())
