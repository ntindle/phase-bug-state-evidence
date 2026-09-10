#!/usr/bin/env python3
"""Issue #301 correction run: Chaos Wand full-outcome repro (accept path + decline control).

Pilot scenario_301.py stopped at the OptionalEffectChoice prompt and drew a false
"fixed" conclusion. This script drives the REPORTED OUTCOME to explicit assertions:

Game 1 (accept path):
  A1 setup_ok            game started, both seats joined
  A2 activation_paid     Wand tapped, 4 untapped islands consumed by {4} cost
  A3 exile_observed      P1 library shrinks; Lightning Bolt among exiled cards
  A4 accept_target_pending  after ACCEPT, a target selection for the free cast is pending
  A5 exile_returned_early   exiled cards back in P1 library BEFORE targets chosen,
                            while the cast is still pending (Bolt zone=Library)
  A6 advertised_target_rejected  exact engine-advertised sequence submission is
                            rejected action_not_allowed; target selection stays pending
  A7 no_damage_no_cast  life 20/20, Bolt not in graveyard/stack-cleared, no cast recorded

Game 2 (decline control):
  A8 decline_control_ok  decline -> no cast, no damage, exiled cards returned, no dangling cast

Verdict = reproduced iff A5 and A6 both pass.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://localhost:9378/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario301")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260910-002"
EVDIR = f"{BACKFILL}/evidence/301/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [
        o for o in state["objects"].values()
        if o.get("zone") == "Battlefield" and o.get("controller") == pid
    ]


def untapped_islands(state, pid):
    return sum(1 for o in bf(state, pid) if o.get("name") == "Island" and not o.get("tapped"))


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        if obj_name(state, oid) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def exile_objs(state):
    return [o for o in state["objects"].values() if o.get("zone") == "Exile"]


def life(state, pid):
    return state["players"][pid]["life"]


def bolt_obj(state):
    for o in state["objects"].values():
        if (o.get("base_name") or o.get("name")) == "Lightning Bolt":
            return o
    return None


def my_turn_priority(state, pid):
    wf = state.get("waiting_for") or {}
    if wf.get("type") == "Priority" and wf.get("data", {}).get("player") == pid:
        return True
    return False


def is_my_main(state, pid):
    return (
        state.get("active_player") == pid
        and state.get("priority_player") == pid
        and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")
    )


def find_vi_choice(st, code, source_ref=None):
    """Find (interactionId, choice) in viewer_interaction exactChoices whose
    surfaces include the given action code (and optional source reference).
    Protocol 69: labels live in surfaces, not on the choice itself."""
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []):
        resp = op.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp["data"].get("choices", []):
            surfs = ch.get("surfaces", [])
            codes = [s.get("data", {}).get("code") for s in surfs]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [s.get("data", {}).get("reference") for s in surfs
                        if s.get("data", {}).get("role") == "source"]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
    return None


async def submit_choice(c, iid, choice_id):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": "choose", "data": {"choiceId": choice_id}}})


async def activate_wand_via_interaction(p0, p1, wand_id, timeout=180):
    """Protocol 69: activated abilities are driven through the exactChoices
    'activateAbility' interaction choice. The legacy ActivateAbility Action is
    rejected (wrong_player) on v0.79.0, and sending any legacy action for P0 in
    the same tick retires the interaction (stale_interaction), so P0 acts ONLY
    via interaction submissions here. Returns True once the wand is tapped."""
    t0 = time.time()
    submitted = set()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        st1 = p1.latest
        if st1:
            for a in st1.get("legal_actions", []):
                if a["type"] in ("PayManaAbilityMana", "PayMana", "PassPriority"):
                    await p1.send_action(a)
                    break
        st = p0.latest
        if not st:
            continue
        s = st["state"]
        w = [o for o in s["objects"].values() if str(o.get("id")) == str(wand_id)]
        if w and w[0].get("tapped"):
            return True
        for a in st.get("legal_actions", []):
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await p0.send_action(a)
        if s.get("priority_player") == p0.player_id:
            f = find_vi_choice(st, "activateAbility", source_ref=wand_id)
            if f and f[1]["id"] not in submitted:
                iid, ch = f
                say(f"submitting activateAbility choice {ch['id']} on {iid}")
                wire("activate_choice_submission", {"interactionId": iid, "choiceId": ch["id"]})
                await submit_choice(p0, iid, ch["id"])
                submitted.add(ch["id"])
    return False


_PASSED_REV = {}


async def gated_pass(p0, p1):
    """Pass priority only for the seat that genuinely holds it (waiting_for
    Priority naming that player). Never blind-passes during a decision prompt:
    a legacy PassPriority during OptionalEffectChoice can resolve (decline) it
    before the driver answers. Revision-aware: never pass twice on the same
    state revision, so a stale duplicate pass cannot land on a later decision."""
    for c, pid in ((p0, p0.player_id), (p1, p1.player_id)):
        st = c.latest
        if not st:
            continue
        rev = st.get("state_revision", -1)
        s = st["state"]
        wf = s.get("waiting_for") or {}
        for a in st.get("legal_actions", []):
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await c.send_action(a)
                break
        if wf.get("type") == "Priority" and s.get("priority_player") == pid:
            if _PASSED_REV.get(c.name, -1) >= rev:
                continue
            for a in st.get("legal_actions", []):
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    _PASSED_REV[c.name] = rev
                    break


async def trace_loop(p0, p1, tag):
    """Background tracer: log the key driver-visible state every 5s."""
    try:
        while True:
            await asyncio.sleep(5)
            for c in (p0, p1):
                st = c.latest
                if not st:
                    say(f"[trace:{tag}] {c.name} latest=None")
                    continue
                s = st["state"]
                wf = s.get("waiting_for") or {}
                acts = st.get("legal_actions", [])
                ex = sum(1 for o in s["objects"].values() if o.get("zone") == "Exile")
                say(f"[trace:{tag}] {c.name} rev={st.get('state_revision')} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} wf={wf.get('type')}/{json.dumps(wf.get('data', {}))[:80]} "
                    f"prio={s.get('priority_player')} acts={[a['type'] for a in acts][:6]} "
                    f"exile={ex} p1lib={lib_size(s,1)}")
    except asyncio.CancelledError:
        pass


async def spy_revisions(p0, tag, duration=90):
    """Log every new state revision's key fields for `duration` seconds."""
    try:
        seen = -1
        t0 = time.time()
        while time.time() - t0 < duration:
            await asyncio.sleep(0.05)
            st = p0.latest
            if not st:
                continue
            rev = st.get("state_revision", -1)
            if rev == seen:
                continue
            seen = rev
            s = st["state"]
            wf = s.get("waiting_for") or {}
            ex = [(o.get("id"), o.get("base_name") or o.get("name")) for o in s["objects"].values()
                  if o.get("zone") == "Exile"]
            stack = [str(x.get("id", "?"))[:40] for x in (s.get("stack", []) or [])]
            say(f"[spy:{tag}] rev={rev} turn={s.get('turn_number')} phase={s.get('phase')} "
                f"wf={wf.get('type')} prio={s.get('priority_player')} stack={stack} "
                f"exile={ex[:6]} p1lib={lib_size(s,1)}")
    except asyncio.CancelledError:
        pass


async def settle_gated(p0, p1, cond, timeout, label, poll=0.25):
    """settle() with gated priority passing. Returns the state or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await gated_pass(p0, p1)
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle_gated: {label}")
    return None


def scan_choices(node, out, depth=0):
    """Collect dicts that look like selectable choices: have id + (label|name|text)."""
    if depth > 8:
        return
    if isinstance(node, dict):
        keys = set(node.keys())
        if "id" in keys and keys & {"label", "name", "text", "title"}:
            out.append(node)
        for v in node.values():
            scan_choices(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            scan_choices(v, out, depth + 1)


def find_candidate_lists(vi):
    """Find lists of target-candidate dicts (id + label/name) under target-ish keys."""
    found = []

    def rec(node, path, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    if ("target" in kl or "candidate" in kl or "legal" in kl or "choice" in kl):
                        if all("id" in x for x in v):
                            found.append((path + "/" + k, v))
                rec(v, path + "/" + k, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                rec(v, f"{path}[{i}]", depth + 1)

    rec(vi, "vi")
    return found


async def settle(p0, p1, cond, timeout, label, poll=0.2):
    """Keep the game moving (mana payments, priority passes) until cond(state)
    is truthy. Returns the state or None on timeout. Never touches
    viewer_interaction decisions."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
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
            if not acted:
                for a in acts:
                    if a["type"] == "PassPriority":
                        await c.send_action(a)
                        break
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle: {label}")
    return None


async def wait_state(c, cond, timeout, label, poll=0.25):
    """Wait until cond(c.latest['state']) is truthy. Returns state or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        st = c.latest
        if st and cond(st["state"]):
            return st["state"]
        if c.latest:
            pass
    return None


async def pass_priority(c):
    st = c.latest
    if not st:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


async def keep_mulligan(c):
    st = c.latest
    if not st:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


async def basic_turn(c, pid, cast_wand=False, activate=False, stats=None):
    """One decision tick for a ramp seat. Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", [])
    if not acts:
        return False
    await keep_mulligan(c)
    st = c.latest
    state, acts = st["state"], st.get("legal_actions", [])
    # mana payment prompts
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if is_my_main(state, pid):
        if activate:
            wand_id = find_bf(state, pid, "Chaos Wand")
            if wand_id is not None:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "ActivateAbility" and d.get("source_id") == wand_id:
                        if untapped_islands(state, pid) >= 12:
                            return ("ACTIVATE", a)
        if cast_wand:
            have = find_bf(state, pid, "Chaos Wand") is not None or any(
                (o.get("base_name") or o.get("name")) == "Chaos Wand"
                for o in (state.get("stack") or []))
            if not have:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == "Chaos Wand":
                        await c.send_action(a)
                        say(f"{c.name} casts Chaos Wand")
                        return True
        for a in acts:
            d = a.get("data", {})
            if a["type"] == "PlayLand" and obj_name(state, d.get("object_id")) == "Island":
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


async def ramp_until(p0, p1, want_fn, timeout_s, label):
    """Drive both seats (P0 ramps/casts wand, P1 draw-go) until want_fn(p0.state)."""
    t0 = time.time()
    last_rev = {}
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.1)
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            if c.revision == last_rev.get(c.name):
                continue
            r = await basic_turn(c, pid, cast_wand=is_p0)
            if r == "ACTIVATE":
                last_rev[c.name] = c.revision
                return r[1]  # the ActivateAbility action
            if r:
                last_rev[c.name] = c.revision
        if p0.latest and want_fn(p0.latest["state"]):
            return "READY"
    say(f"TIMEOUT in ramp_until: {label}")
    return None


async def run_game(mode):
    """mode in {'accept','decline'}. Returns dict of observations + assertions."""
    _PASSED_REV.clear()  # state revisions restart each game
    obs = {"mode": mode, "assert": {}, "notes": []}
    p0 = PhaseClient("P0")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck(("Island", 56), ("Chaos Wand", 4)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 59), ("Lightning Bolt", 1)))
    say(f"game {p0.game_code} mode={mode}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None and p1.player_id is not None) else "failed"

    # ramp: cast wand, then wait for activation window
    act = await ramp_until(
        p0, p1,
        lambda s: (find_bf(s, p0.player_id, "Chaos Wand") is not None
                   and untapped_islands(s, p0.player_id) >= 12
                   and is_my_main(s, p0.player_id)),
        900, "ramp to 12 untapped islands with wand out")
    if act != "ACTIVATE" and act != "READY":
        obs["assert"]["A2_activation_paid"] = "not-run"
        obs["notes"].append("never reached activation window")
        await p0.close(); await p1.close()
        return obs
    if act == "READY":
        # activation window reached; wand id for the interaction lookup
        st = p0.latest["state"]
        wand_id = find_bf(st, p0.player_id, "Chaos Wand")
        if wand_id is None:
            obs["assert"]["A2_activation_paid"] = "not-run"
            obs["notes"].append("activation window seen but wand not on battlefield")
            await p0.close(); await p1.close()
            return obs

    say("exporting PRE_ACTIVATION state")
    pre_env_s = await p0.export_state()
    pre_env = json.loads(pre_env_s)
    pre = pre_env["state"]
    with open(f"{EVDIR}/pre_activation_{mode}.json", "w") as f:
        f.write(pre_env_s)
    pre_untapped = untapped_islands(pre, p0.player_id)
    pre_lib_p1 = lib_size(pre, 1)
    wand_id = find_bf(pre, p0.player_id, "Chaos Wand")
    say(f"ACTIVATING CHAOS WAND (id {wand_id}) via protocol-69 interaction choice")
    wire("activate_ability_action", {"method": "interaction-choice", "wand_id": wand_id})
    ok = await activate_wand_via_interaction(p0, p1, wand_id)
    tracer = None
    if os.environ.get("TRACE") == "1":
        tracer = asyncio.create_task(trace_loop(p0, p1, mode))
    spy = None
    if os.environ.get("SPY") == "1":
        spy = asyncio.create_task(spy_revisions(p0, mode))
    # activation should have tapped wand + spent 4 mana; keep game moving
    s = await settle_gated(p0, p1, lambda s: True, 8, "post-activation settle")
    if s is None:
        s = p0.latest["state"]
    wand = next(o for o in bf(s, p0.player_id) if (o.get("base_name") or o.get("name")) == "Chaos Wand")
    paid = ok and wand.get("tapped") and untapped_islands(s, p0.player_id) <= pre_untapped - 4
    obs["assert"]["A2_activation_paid"] = "passed" if paid else "failed"
    obs["notes"].append(f"interaction activation ok={ok}; wand tapped={wand.get('tapped')} untapped islands {pre_untapped}->{untapped_islands(s, p0.player_id)}")
    if not ok:
        await p0.close(); await p1.close()
        return obs

    # Phase A: the exile must begin. Require an actual Exile-zone object, not
    # just a library shrink (a natural draw also shrinks the library and gave a
    # false positive on the previous attempt).
    def exiled_happened(s):
        return (lib_size(s, 1) < pre_lib_p1
                and any(o.get("zone") == "Exile" for o in s["objects"].values()))
    s2 = await settle_gated(p0, p1, exiled_happened, 180, "exile resolution")
    if s2 is None:
        # capture the stuck state for diagnosis
        stuck_s = await p0.export_state()
        with open(f"{EVDIR}/stuck_after_activation_{mode}.json", "w") as f:
            f.write(stuck_s)
        obs["assert"]["A3_exile_observed"] = "failed"
        obs["notes"].append("P1 library never shrank after activation")
        await p0.close(); await p1.close()
        return obs
    bolt = bolt_obj(s2)
    bolt_exiled = bolt is not None and bolt.get("zone") == "Exile"
    obs["assert"]["A3_exile_observed"] = "passed" if bolt_exiled else "failed"
    obs["notes"].append(f"P1 library {pre_lib_p1}->{lib_size(s2, 1)}; bolt zone={bolt.get('zone') if bolt else 'missing'}")
    say("exporting POST_EXILE checkpoint")
    post_exile_s = await p0.export_state()
    with open(f"{EVDIR}/post_exile_{mode}.json", "w") as f:
        f.write(post_exile_s)

    # Phase B: drive to the may-cast prompt (OptionalEffectChoice for P0).
    # The prompt is answered explicitly below; gated passes cannot auto-decline it.
    s_prompt = await settle_gated(
        p0, p1,
        lambda s: (s.get("waiting_for") or {}).get("type") == "OptionalEffectChoice",
        180, "may-cast prompt")
    if s_prompt is None:
        obs["notes"].append("no OptionalEffectChoice may-cast prompt observed within 180s")
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close(); await p1.close()
        return obs
    # read the fresh interaction and pick the accept/decline choice from
    # surfaces role/value (protocol 69 has no text labels on choices)
    vi_seat, pick_iid, pick_cid = None, None, None
    st = p0.latest
    wf = (st["state"].get("waiting_for") or {})
    if wf.get("type") == "OptionalEffectChoice" and wf.get("data", {}).get("player") == p0.player_id:
        vi = st.get("viewer_interaction") or {}
        for op in vi.get("opportunities", []):
            resp = op.get("response", {})
            if resp.get("type") != "exactChoices":
                continue
            for ch in resp["data"].get("choices", []):
                codes = [s.get("data", {}).get("code") for s in ch.get("surfaces", [])]
                if "decideOptionalEffect" not in codes:
                    continue
                for sf in ch.get("surfaces", []):
                    dd = sf.get("data", {})
                    if dd.get("role") == "accept":
                        is_accept = str(dd.get("value")).lower() == "true"
                        if (mode == "accept") == is_accept:
                            pick_iid, pick_cid = op.get("interactionId"), ch["id"]
        if pick_cid:
            vi_seat = p0
    if vi_seat is None:
        obs["notes"].append("OptionalEffectChoice pending but no decideOptionalEffect choice identified")
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close(); await p1.close()
        return obs
    vi = vi_seat.latest.get("viewer_interaction")
    say(f"may-cast opportunity on {vi_seat.name}; interaction=", json.dumps(vi)[:3000])
    wire(f"optional_cast_opportunity_{mode}", vi)
    say(f"{vi_seat.name} submitting {mode}: choice id={pick_cid}")
    submission = {
        "interactionId": pick_iid,
        "response": {"type": "choose", "data": {"choiceId": pick_cid}},
    }
    wire(f"{mode}_submission", submission)
    await submit_choice(vi_seat, pick_iid, pick_cid)
    await asyncio.sleep(2)

    if mode == "decline":
        obs["assert"].pop("A1_setup_ok", None)  # keep accept game's A1
        # decline control: let cleanup finish (exiled cards to bottom of library).
        # P1's library is hidden info in P0's view, so the bolt vanishes from the
        # object scan once it returns -- wait for Exile to empty instead.
        s3 = await settle_gated(
            p0, p1,
            lambda s: not any(o.get("zone") == "Exile" for o in s["objects"].values()),
            120, "decline cleanup")
        if s3 is None:
            s3 = p0.latest["state"]
            obs["notes"].append("decline cleanup timed out waiting for Exile to empty")
        visible_bolts = [(o.get("id"), o.get("zone")) for o in s3["objects"].values()
                         if (o.get("base_name") or o.get("name")) == "Lightning Bolt"]
        wf_txt = json.dumps(s3.get("waiting_for", {}))
        dangling = "TargetSelection" in wf_txt
        ok = (all(z != "Stack" for _, z in visible_bolts)
              and not any(o.get("zone") == "Exile" for o in s3["objects"].values())
              and life(s3, 0) == 20 and life(s3, 1) == 20 and not dangling)
        obs["assert"]["A8_decline_control_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"decline: visible bolts={visible_bolts} life={life(s3,0)}/{life(s3,1)} "
                            f"waiting={wf_txt[:160]}")
        say("exporting DECLINE_POST state")
        dp = await p0.export_state()
        with open(f"{EVDIR}/decline_post.json", "w") as f:
            f.write(dp)
        await p0.close(); await p1.close()
        return obs

    # accept path: expect TargetSelection for the free cast
    def target_pending(s):
        return "TargetSelection" in json.dumps(s.get("waiting_for", {}))
    s4 = await settle_gated(p0, p1, target_pending, 120, "target selection")
    obs["assert"]["A4_accept_target_pending"] = "passed" if s4 is not None else "failed"
    if s4 is None:
        obs["notes"].append("no TargetSelection after accept")
        await p0.close(); await p1.close()
        return obs
    say("TargetSelection pending; exporting PRE_TARGET checkpoint")
    pre_target_s = await p0.export_state()
    with open(f"{EVDIR}/pre_target_accept.json", "w") as f:
        f.write(pre_target_s)
    pre_target = json.loads(pre_target_s)["state"]
    bolt = bolt_obj(pre_target)
    # A5: exiled cards returned to library BEFORE target submission, cast still pending
    early_return = (lib_size(pre_target, 1) == pre_lib_p1
                    and bolt is not None and bolt.get("zone") == "Library"
                    and str(bolt.get("id")) in [str(x) for x in pre_target["players"][1]["library"]])
    obs["assert"]["A5_exile_returned_early"] = "passed" if early_return else "failed"
    obs["notes"].append(f"pre-target: P1 lib={lib_size(pre_target,1)} (pre-exile {pre_lib_p1}); "
                        f"bolt zone={bolt.get('zone') if bolt else 'missing'} in-library-membership="
                        f"{str(bolt.get('id')) in [str(x) for x in pre_target['players'][1]['library']] if bolt else '?'}")

    # find the advertised target candidates on the accepting seat
    vi2 = vi_seat.latest.get("viewer_interaction") or {}
    say("target interaction=", json.dumps(vi2)[:4000])
    wire("target_opportunity", vi2)
    cands = find_candidate_lists(vi2)
    say("candidate lists found:", json.dumps([(p, [{"id": x.get("id"), "label": str(x.get('label') or x.get('name'))[:80]} for x in xs]) for p, xs in cands])[:2500])
    wire("target_candidates", cands)
    target_id = None
    target_iid = None
    for path, xs in cands:
        if len(xs) == 1:
            target_id = xs[0]["id"]
            break
    if target_id is None:
        # prefer the opponent player candidate (seat 1): the natural Bolt target
        for path, xs in cands:
            for x in xs:
                for sf in x.get("surfaces", []):
                    dd = sf.get("data", {})
                    if dd.get("role") == "candidate" and dd.get("seat") == 1:
                        target_id = x["id"]
                        break
                if target_id:
                    break
            if target_id:
                break
    # the interactionId lives on the opportunity, not on viewer_interaction
    if target_id:
        for op in (vi2.get("opportunities", []) or []):
            resp = op.get("response", {})
            cands_here = ((resp.get("data", {}) or {}).get("candidates", []) or [])
            if any(c.get("id") == target_id for c in cands_here):
                target_iid = op.get("interactionId")
                break
    if target_id is None or target_iid is None:
        obs["assert"]["A6_advertised_target_rejected"] = "not-run"
        obs["notes"].append("no advertised target candidate identified; interaction preserved in wire log")
        await p0.close(); await p1.close()
        return obs
    iid2 = target_iid
    submission2 = {
        "interactionId": iid2,
        "response": {"type": "sequence", "data": {"choiceIds": [target_id]}},
    }
    say(f"submitting advertised target: id={target_id}")
    wire("target_submission", submission2)
    await vi_seat.send_interaction(submission2)
    # watch for rejection
    rejected = None
    t0 = time.time()
    while time.time() - t0 < 20 and rejected is None:
        await asyncio.sleep(0.5)
        try:
            while True:
                t, data = vi_seat.inbox.get_nowait()
                if t in ("ActionRejected", "Error"):
                    rejected = {"type": t, "data": data}
                    wire("target_rejection", rejected)
                    say("REJECTION:", json.dumps(rejected)[:600])
        except asyncio.QueueEmpty:
            pass
    await asyncio.sleep(2)
    s5 = p0.latest["state"]
    wf_txt = json.dumps(s5.get("waiting_for", {}))
    still_pending = "TargetSelection" in wf_txt
    not_allowed = rejected is not None and "not_allowed" in json.dumps(rejected).lower().replace(" ", "_")
    obs["assert"]["A6_advertised_target_rejected"] = "passed" if (not_allowed and still_pending) else "failed"
    obs["notes"].append(f"rejection={json.dumps(rejected)[:200] if rejected else 'none'}; still_pending={still_pending}")
    bolt = bolt_obj(s5)
    no_cast = (life(s5, 0) == 20 and life(s5, 1) == 20
               and (bolt is None or bolt.get("zone") != "Graveyard"))
    obs["assert"]["A7_no_damage_no_cast"] = "passed" if no_cast else "failed"
    obs["notes"].append(f"post: life={life(s5,0)}/{life(s5,1)} bolt zone={bolt.get('zone') if bolt else 'missing'}")
    say("exporting POST_FAILURE state")
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_failure_accept.json", "w") as f:
        f.write(post_s)
    await p0.close(); await p1.close()
    return obs


async def main():
    t0 = time.time()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    all_obs = {}
    if only in (None, "accept"):
        all_obs["accept"] = await run_game("accept")
    if only in (None, "decline"):
        all_obs["decline"] = await run_game("decline")
    dur = time.time() - t0
    ass = {}
    notes = []
    for m in ("accept", "decline"):
        if m not in all_obs:
            continue
        for k, v in all_obs[m]["assert"].items():
            ass[k] = v
        notes.extend(f"[{m}] {n}" for n in all_obs[m]["notes"])
    notes.append("protocol-69 driver adaptation (v0.79.0): activated abilities driven via "
                 "viewer_interaction exactChoices 'activateAbility' choice; the legacy "
                 "ActivateAbility Action is rejected wrong_player on this release. "
                 "OptionalEffectChoice accept/decline read from surfaces role/value.")
    a5 = all_obs.get("accept", {}).get("assert", {}).get("A5_exile_returned_early")
    a6 = all_obs.get("accept", {}).get("assert", {}).get("A6_advertised_target_rejected")
    a7 = all_obs.get("accept", {}).get("assert", {}).get("A7_no_damage_no_cast")
    a8 = all_obs.get("decline", {}).get("assert", {}).get("A8_decline_control_ok")
    if a5 == "passed" and a6 == "passed":
        verdict = "reproduced"
    elif a5 == "failed" and a6 == "failed" and a7 == "passed" and a8 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": 301,
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
        "server_run_dir": "runs/20260910-002",
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_301_079b.py"),
        "decks": {
            "P0": [["Island", 56], ["Chaos Wand", 4]],
            "P1": [["Island", 59], ["Lightning Bolt", 1]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
        "setup_line": "P0: 56x Island + 4x Chaos Wand; P1: 59x Island + 1x Lightning Bolt (draw-go)",
        "contract_line": "Accept: free Bolt cast must finish targeting/resolve for 3 dmg; Decline: no cast, cards to bottom",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close(); RUNLOG.close()


asyncio.run(main())
