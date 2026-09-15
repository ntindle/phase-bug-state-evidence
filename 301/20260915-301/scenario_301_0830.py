#!/usr/bin/env python3
"""Issue #301 revalidation on v0.83.0 (protocol 70): Chaos Wand accept path.

History: pilot reported a false "fixed"; the correction reproduced the bug on
v0.77.0 and the maintained comment re-validated "still reproduced" on v0.79.0.
The issue is now labeled status:fixed-unreleased. This run re-tests the full
outcome contract on the pinned v0.83.0 (b7a59d4, protocol 70):

Game 1 (accept path):
  A1 setup_ok
  A2 activation_paid      Wand tapped, 4 untapped islands consumed by {4} cost
  A3 exile_observed      P1 library shrinks; Lightning Bolt among exiled cards
  A4 accept_target_pending  after ACCEPT, a target selection for the free cast is pending
  A5 exile_returned_early   exiled cards back in P1 library BEFORE targets chosen,
                            while the cast is still pending (Bolt zone=Library) [bug]
  A6 advertised_target_rejected  exact engine-advertised sequence submission is
                            rejected action_not_allowed; target selection stays pending [bug]
  A7 no_damage_no_cast   life 20/20, Bolt not in graveyard/stack-cleared, no cast recorded [bug]
  A5fx target_accepted   no rejection; TargetSelection clears after submission [fixed]
  A6fx cast_resolved     Bolt resolves: P1 at 17 life, Bolt in P1 graveyard,
                         no exile, no dangling cast [fixed]

Game 2 (decline control):
  A8 decline_control_ok  decline -> no cast, no damage, exiled cards returned, no dangling cast

Verdict: reproduced iff A5 and A6 pass;
         not-reproduced iff A5fx, A6fx and A8 pass;
         blocked otherwise.
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
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario301")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260915-301"
EVDIR = f"{BACKFILL}/evidence/301/{EVID_RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
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


SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

# recompute against on-disk artifacts; never copy hashes blindly
for _f, _k in (("server/releases/v0.83.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.83.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.83.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_islands(state, pid):
    return sum(1 for o in bf(state, pid) if o.get("name") == "Island" and not o.get("tapped"))


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def life(state, pid):
    return state["players"][pid]["life"]


def hand_ids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def bolt_obj(state):
    for o in state["objects"].values():
        if (o.get("base_name") or o.get("name")) == "Lightning Bolt":
            return o
    return None


def wf_of(state):
    return state.get("waiting_for") or {}


def merged_actions(st):
    return st.get("legal_actions") or []


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


async def submit_as_is(c, a):
    await c.send_action(a)


async def do_mulligan(c, pid, tag, mulls):
    """Protocol 70: answer the advertised MulliganDecision action as-is with
    data.decision. Gate on the seat's presence in waiting_for.data.pending."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    a = find_action(merged_actions(st), "MulliganDecision")
    if not a or tag in mulls:
        return False
    sub = copy.deepcopy(a)
    sub["data"]["decision"] = "keep"
    say(f"[{tag}] mulligan keep")
    await submit_as_is(c, sub)
    mulls[tag] = True
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag, mulls):
    """BottomCards phase after Declare: SelectCards with count from pending."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    n = ((pend.get("phase") or {}).get("count")) or 1
    a = find_action(merged_actions(st), "SelectCards")
    if not a or (tag, "bottomed") in mulls:
        return False
    picks = [int(x) for x in hand_ids(state, pid)[:n]]
    sub = copy.deepcopy(a)
    sub["data"]["cardIds"] = picks
    say(f"[{tag}] bottoming {n}: {[obj_name(state, x) for x in picks]}")
    await submit_as_is(c, sub)
    mulls[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def pay_tick(acts, c, tag):
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and str((wf.get("data") or {}).get("player")) == str(pid)


async def pass_priority(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            return True
    return False


_PASSED_REV = {}


async def gated_pass(p0, p1):
    """Pass priority only for the seat that genuinely holds it (waiting_for
    Priority naming that player). Never blind-pass during a decision prompt:
    a PassPriority during OptionalEffectChoice can resolve (decline) it before
    the driver answers. Revision-aware: never pass twice on the same revision."""
    for c, pid in ((p0, p0.player_id), (p1, p1.player_id)):
        st = c.latest
        if not st:
            continue
        rev = st.get("state_revision", -1)
        s = st["state"]
        wf = wf_of(s)
        for a in merged_actions(st):
            if a.get("type") in ("PayManaAbilityMana", "PayMana"):
                await c.send_action(a)
                break
        if wf.get("type") == "Priority" and str((wf.get("data") or {}).get("player")) == str(pid):
            if _PASSED_REV.get(c.name, -1) >= rev:
                continue
            for a in merged_actions(st):
                if a.get("type") == "PassPriority":
                    await c.send_action(a)
                    _PASSED_REV[c.name] = rev
                    break


async def settle_gated(p0, p1, cond, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await gated_pass(p0, p1)
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle_gated: {label}")
    return None


def find_vi_choice(st, code, source_ref=None):
    """Find (interactionId, choice) in viewer_interaction exactChoices whose
    surfaces include the given action code (and optional source reference)."""
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
            return op.get("interactionId") or op.get("id"), ch
    return None


async def submit_choice(c, iid, choice_id):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": "choose", "data": {"choiceId": choice_id}}})


async def activate_wand(p0, p1, wand_id, mulls, timeout=240):
    """Protocol 70: submit the advertised ActivateAbility action (source_id +
    ability_index) as-is. Fall back to the protocol-69 interaction workaround
    if no such action is advertised. Returns True once the wand is tapped."""
    t0 = time.time()
    submitted = set()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        for c, pid, tag in ((p1, p1.player_id, "P1"), (p0, p0.player_id, "P0")):
            st = c.latest
            if not st:
                continue
            acts = merged_actions(st)
            await pay_tick(acts, c, tag)
            if wf_of(st["state"]).get("type") == "MulliganDecision":
                await do_mulligan(c, pid, tag, mulls)
                continue
            await do_bottom(c, pid, tag, mulls)
        st = p0.latest
        if not st:
            continue
        s = st["state"]
        w = [o for o in s["objects"].values() if str(o.get("id")) == str(wand_id)]
        if w and w[0].get("tapped"):
            return True
        acts = merged_actions(st)
        act = next((a for a in acts
                    if a.get("type") == "ActivateAbility"
                    and str((a.get("data") or {}).get("source_id")) == str(wand_id)), None)
        if act is None:
            act = next((a for a in acts if a.get("type") == "ActivateAbility"), None)
        if act is not None:
            key = json.dumps(act, sort_keys=True)
            if key not in submitted:
                say(f"submitting ActivateAbility {json.dumps(act.get('data'))[:120]}")
                wire("activate_ability_action", {"action": act})
                await submit_as_is(p0, act)
                submitted.add(key)
                continue
        if my_priority(s, p0.player_id):
            f = find_vi_choice(st, "activateAbility", source_ref=wand_id)
            if f and f[1]["id"] not in submitted:
                iid, ch = f
                say(f"fallback: submitting activateAbility choice {ch['id']} on {iid}")
                wire("activate_choice_submission", {"interactionId": iid, "choiceId": ch["id"]})
                await submit_choice(p0, iid, ch["id"])
                submitted.add(ch["id"])
    return False


def find_candidate_lists(vi):
    found = []

    def rec(node, path, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    if "target" in kl or "candidate" in kl or "legal" in kl or "choice" in kl:
                        if all("id" in x for x in v):
                            found.append((path + "/" + k, v))
                rec(v, path + "/" + k, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                rec(v, f"{path}[{i}]", depth + 1)

    rec(vi, "vi")
    return found


def choice_text(choice):
    surfs = choice.get("surfaces", [])
    bits = []
    for s in surfs:
        d = s.get("data") or {}
        for k in ("text", "label", "name", "value", "code"):
            if d.get(k):
                bits.append(f"{d.get('role','?')}:{k}={d[k]}")
    return " | ".join(bits)


async def pick_optional_cast(c, want_accept):
    """Answer the pending OptionalEffectChoice: pick by surfaces role/value
    (role accept, value true/false); fall back to text matching."""
    st = c.latest
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []):
        resp = op.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp["data"].get("choices", []):
            codes = [s.get("data", {}).get("code") for s in ch.get("surfaces", [])]
            if "decideOptionalEffect" not in codes:
                continue
            is_accept = None
            for sf in ch.get("surfaces", []):
                dd = sf.get("data", {})
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                txt = choice_text(ch).lower()
                if "cast" in txt or "accept" in txt:
                    is_accept = True
                elif "decline" in txt or "don't" in txt or "do not" in txt:
                    is_accept = False
            if is_accept == want_accept:
                return op.get("interactionId") or op.get("id"), ch["id"]
    return None, None


async def basic_turn(c, pid, tag, mulls, want_wand=False, want_activate=False):
    """One decision tick for a ramp seat. Returns True if it acted, or
    ('ACTIVATE', wand_id) when the activation window is reached."""
    st = c.latest
    if not st:
        return False
    if await do_mulligan(c, pid, tag, mulls):
        return True
    if await do_bottom(c, pid, tag, mulls):
        return True
    st = c.latest
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(acts, c, tag):
        return True
    if my_main(state, pid):
        if want_activate:
            wand_id = find_bf(state, pid, "Chaos Wand")
            if wand_id is not None and untapped_islands(state, pid) >= 12:
                return ("ACTIVATE", wand_id)
        if want_wand:
            have = find_bf(state, pid, "Chaos Wand") is not None or any(
                (o.get("base_name") or o.get("name")) == "Chaos Wand"
                for o in (state.get("stack") or []))
            if not have:
                a = next((a for a in acts if a.get("type") == "CastSpell"
                          and obj_name(state, (a.get("data") or {}).get("object_id")) == "Chaos Wand"), None)
                if a:
                    await submit_as_is(c, a)
                    say(f"{c.name} casts Chaos Wand")
                    return True
        for a2 in acts:
            if a2.get("type") == "PlayLand" and obj_name(state, (a2.get("data") or {}).get("object_id")) == "Island":
                await submit_as_is(c, a2)
                return True
    for a in acts:
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            return True
    return False


async def ramp_until(p0, p1, mulls, timeout_s, label):
    t0 = time.time()
    last_rev = {}
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.1)
        for c, pid, tag, is_p0 in ((p0, p0.player_id, "P0", True),
                                   (p1, p1.player_id, "P1", False)):
            if c.revision == last_rev.get(c.name):
                continue
            r = await basic_turn(c, pid, tag, mulls, want_wand=is_p0, want_activate=is_p0)
            if isinstance(r, tuple) and r[0] == "ACTIVATE":
                last_rev[c.name] = c.revision
                return r
            if r:
                last_rev[c.name] = c.revision
        if p0.latest and my_main(p0.latest["state"], p0.player_id) \
                and find_bf(p0.latest["state"], p0.player_id, "Chaos Wand") is not None \
                and untapped_islands(p0.latest["state"], p0.player_id) >= 12:
            return ("ACTIVATE", find_bf(p0.latest["state"], p0.player_id, "Chaos Wand"))
    say(f"TIMEOUT in ramp_until: {label}")
    return None

async def run_game(mode):
    """mode in {'accept','decline'}. Returns dict of observations + assertions."""
    _PASSED_REV.clear()
    mulls = {}
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

    act = await ramp_until(p0, p1, mulls, 900, "ramp to 12 untapped islands with wand out")
    if not act or act[0] != "ACTIVATE":
        obs["assert"]["A2_activation_paid"] = "not-run"
        obs["notes"].append("never reached activation window")
        await p0.close(); await p1.close()
        return obs
    wand_id = act[1]

    say("exporting PRE_ACTIVATION state")
    pre_env_s = await p0.export_state()
    pre_env = json.loads(pre_env_s)
    pre = pre_env["state"]
    with open(f"{EVDIR}/pre_activation_{mode}.json", "w") as f:
        f.write(pre_env_s)
    pre_untapped = untapped_islands(pre, p0.player_id)
    pre_lib_p1 = lib_size(pre, 1)
    wand_id = find_bf(pre, p0.player_id, "Chaos Wand")
    say(f"ACTIVATING CHAOS WAND (id {wand_id}) via advertised ActivateAbility")
    ok = await activate_wand(p0, p1, wand_id, mulls)
    s = await settle_gated(p0, p1, lambda s: True, 8, "post-activation settle")
    if s is None:
        s = p0.latest["state"]
    wand = next(o for o in bf(s, p0.player_id) if (o.get("base_name") or o.get("name")) == "Chaos Wand")
    paid = ok and wand.get("tapped") and untapped_islands(s, p0.player_id) <= pre_untapped - 4
    obs["assert"]["A2_activation_paid"] = "passed" if paid else "failed"
    obs["notes"].append(f"activation ok={ok}; wand tapped={wand.get('tapped')} untapped islands {pre_untapped}->{untapped_islands(s, p0.player_id)}")
    if not ok:
        await p0.close(); await p1.close()
        return obs

    # The exile must begin: require an actual Exile-zone object, not just a
    # library shrink (a natural draw also shrinks the library).
    def exiled_happened(s):
        return (lib_size(s, 1) < pre_lib_p1
                and any(o.get("zone") == "Exile" for o in s["objects"].values()))
    s2 = await settle_gated(p0, p1, exiled_happened, 180, "exile resolution")
    if s2 is None:
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

    # Drive to the may-cast prompt (OptionalEffectChoice for P0). gated passes
    # cannot auto-decline it; it is answered explicitly below.
    s_prompt = await settle_gated(
        p0, p1,
        lambda s: wf_of(s).get("type") == "OptionalEffectChoice"
        and str(((wf_of(s).get("data") or {}).get("player"))) == str(p0.player_id),
        180, "may-cast prompt")
    if s_prompt is None:
        obs["notes"].append("no OptionalEffectChoice may-cast prompt observed within 180s")
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A5fx_target_accepted",
                  "A6fx_cast_resolved", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close(); await p1.close()
        return obs
    pick_iid, pick_cid = await pick_optional_cast(p0, want_accept=(mode == "accept"))
    if not pick_cid:
        vi = p0.latest.get("viewer_interaction")
        say("OptionalEffectChoice pending but no decideOptionalEffect choice identified; interaction=",
            json.dumps(vi)[:4000])
        wire(f"optional_cast_opportunity_{mode}_unanswered", vi)
        for k in ("A4_accept_target_pending", "A5_exile_returned_early",
                  "A6_advertised_target_rejected", "A5fx_target_accepted",
                  "A6fx_cast_resolved", "A7_no_damage_no_cast"):
            obs["assert"].setdefault(k, "not-run")
        obs["notes"].append("may-cast choice not identified")
        await p0.close(); await p1.close()
        return obs
    wire(f"optional_cast_opportunity_{mode}", p0.latest.get("viewer_interaction"))
    say(f"P0 submitting {mode}: choice id={pick_cid}")
    submission = {"interactionId": pick_iid,
                  "response": {"type": "choose", "data": {"choiceId": pick_cid}}}
    wire(f"{mode}_submission", submission)
    await submit_choice(p0, pick_iid, pick_cid)
    await asyncio.sleep(2)

    if mode == "decline":
        # decline control: let cleanup finish (exiled cards to bottom of library).
        s3 = await settle_gated(
            p0, p1,
            lambda s: not any(o.get("zone") == "Exile" for o in s["objects"].values()),
            120, "decline cleanup")
        if s3 is None:
            s3 = p0.latest["state"]
            obs["notes"].append("decline cleanup timed out waiting for Exile to empty")
        visible_bolts = [(o.get("id"), o.get("zone")) for o in s3["objects"].values()
                         if (o.get("base_name") or o.get("name")) == "Lightning Bolt"]
        wf_txt = json.dumps(wf_of(s3))
        dangling = "TargetSelection" in wf_txt or "OptionalEffectChoice" in wf_txt
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
        return "TargetSelection" in json.dumps(wf_of(s))
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
    # A5 (bug symptom): exiled cards returned to library BEFORE target submission
    early_return = (lib_size(pre_target, 1) == pre_lib_p1
                    and bolt is not None and bolt.get("zone") == "Library"
                    and str(bolt.get("id")) in [str(x) for x in pre_target["players"][1]["library"]])
    obs["assert"]["A5_exile_returned_early"] = "passed" if early_return else "failed"
    obs["notes"].append(f"pre-target: P1 lib={lib_size(pre_target,1)} (pre-exile {pre_lib_p1}); "
                        f"bolt zone={bolt.get('zone') if bolt else 'missing'} in-library-membership="
                        f"{str(bolt.get('id')) in [str(x) for x in pre_target['players'][1]['library']] if bolt else '?'}")

    # find the advertised target candidates on the accepting seat
    vi2 = p0.latest.get("viewer_interaction") or {}
    say("target interaction=", json.dumps(vi2)[:4000])
    wire("target_opportunity", vi2)
    cands = find_candidate_lists(vi2)
    say("candidate lists found:", json.dumps(
        [(p, [{"id": x.get("id"), "label": str(x.get("label") or x.get("name"))[:80]} for x in xs])
         for p, xs in cands])[:2500])
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
                    if dd.get("role") == "candidate" and str(dd.get("seat")) == "1":
                        target_id = x["id"]
                        break
                if target_id:
                    break
            if target_id:
                break
    if target_id:
        for op in (vi2.get("opportunities", []) or []):
            resp = op.get("response", {})
            resp_kind = resp.get("type")
            data = resp.get("data", {}) or {}
            cand_ids = [c.get("id") for c in (data.get("candidates", []) or [])]
            cand_ids += [c.get("id") for c in (data.get("choices", []) or [])]
            if target_id in cand_ids:
                target_iid = op.get("interactionId") or op.get("id")
                if resp_kind == "exactChoices":
                    # exactChoices: single choice submission
                    submission2 = {"interactionId": target_iid,
                                   "response": {"type": "choose", "data": {"choiceId": target_id}}}
                else:
                    submission2 = {"interactionId": target_iid,
                                   "response": {"type": "sequence", "data": {"choiceIds": [target_id]}}}
                break
    if target_id is None or target_iid is None:
        obs["assert"]["A6_advertised_target_rejected"] = "not-run"
        obs["assert"]["A5fx_target_accepted"] = "not-run"
        obs["notes"].append("no advertised target candidate identified; interaction preserved in wire log")
        await p0.close(); await p1.close()
        return obs
    say(f"submitting advertised target: id={target_id} kind={submission2['response']['type']}")
    wire("target_submission", submission2)
    await p0.send_interaction(submission2)
    # watch for rejection
    rejected = None
    t0 = time.time()
    while time.time() - t0 < 20 and rejected is None:
        await asyncio.sleep(0.5)
        try:
            while True:
                t, data = p0.inbox.get_nowait()
                if t in ("ActionRejected", "Error"):
                    rejected = {"type": t, "data": data}
                    wire("target_rejection", rejected)
                    say("REJECTION:", json.dumps(rejected)[:600])
        except asyncio.QueueEmpty:
            pass
    await asyncio.sleep(2)
    s5 = p0.latest["state"]
    wf_txt = json.dumps(wf_of(s5))
    still_pending = "TargetSelection" in wf_txt
    not_allowed = rejected is not None and "not_allowed" in json.dumps(rejected).lower().replace(" ", "_")
    obs["assert"]["A6_advertised_target_rejected"] = "passed" if (not_allowed and still_pending) else "failed"
    obs["notes"].append(f"rejection={json.dumps(rejected)[:200] if rejected else 'none'}; still_pending={still_pending}")

    if not_allowed and still_pending:
        # bug path: record the failure end state
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

    # fixed path: target accepted; drive to resolution and assert the cast completes
    obs["assert"]["A5fx_target_accepted"] = "passed" if (rejected is None and not still_pending) else "failed"
    obs["notes"].append(f"fixed-path entry: rejected={bool(rejected)} still_pending={still_pending}")

    def resolved(s):
        b = bolt_obj(s)
        return (b is not None and b.get("zone") == "Graveyard"
                and life(s, 1) == 17
                and not any(o.get("zone") == "Exile" for o in s["objects"].values()))
    s6 = await settle_gated(p0, p1, resolved, 120, "bolt resolution")
    bolt = bolt_obj(s6) if s6 else bolt_obj(p0.latest["state"])
    s6 = s6 or p0.latest["state"]
    ok_fx = (bolt is not None and bolt.get("zone") == "Graveyard"
             and life(s6, 1) == 17 and life(s6, 0) == 20
             and not any(o.get("zone") == "Exile" for o in s6["objects"].values())
             and "TargetSelection" not in json.dumps(wf_of(s6)))
    obs["assert"]["A6fx_cast_resolved"] = "passed" if ok_fx else "failed"
    obs["notes"].append(f"post-resolution: life={life(s6,0)}/{life(s6,1)} bolt zone={bolt.get('zone') if bolt else 'missing'} "
                        f"bolt controller={bolt.get('controller') if bolt else '?'}")
    say("exporting POST_SUCCESS state")
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_success_accept.json", "w") as f:
        f.write(post_s)
    await p0.close(); await p1.close()
    return obs


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #301 — Chaos Wand optional cast (revalidation)", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} — {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_activation_paid": "A2 wand activated ({4} paid, tapped)",
        "A3_exile_observed": "A3 Bolt exiled from P1 library",
        "A4_accept_target_pending": "A4 TargetSelection pending after accept",
        "A5_exile_returned_early": "A5 [bug] exile returned to library pre-target",
        "A6_advertised_target_rejected": "A6 [bug] advertised target rejected action_not_allowed",
        "A7_no_damage_no_cast": "A7 [bug] life 20/20, no cast recorded",
        "A5fx_target_accepted": "A5fx [fixed] target submission accepted",
        "A6fx_cast_resolved": "A6fx [fixed] Bolt resolved: P1 17, Bolt in P1 GY",
        "A8_decline_control_ok": "A8 decline control: clean, no dangling cast",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'✓' if v=='passed' else ('✗' if v=='failed' else '–')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:8]:
        d.text((40, y), n[:120], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 301/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


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
    notes.append("protocol-70 driver (v0.83.0): mulligan answered via advertised "
                 "MulliganDecision action as-is (data.decision='keep', gated on "
                 "waiting_for.data.pending[]); activation via advertised "
                 "ActivateAbility action (source_id/ability_index); optional-cast "
                 "accept/decline from viewer_interaction exactChoices surfaces "
                 "role/value; target submission per the advertised response schema.")
    a5 = all_obs.get("accept", {}).get("assert", {}).get("A5_exile_returned_early")
    a6 = all_obs.get("accept", {}).get("assert", {}).get("A6_advertised_target_rejected")
    a5fx = all_obs.get("accept", {}).get("assert", {}).get("A5fx_target_accepted")
    a6fx = all_obs.get("accept", {}).get("assert", {}).get("A6fx_cast_resolved")
    a8 = all_obs.get("decline", {}).get("assert", {}).get("A8_decline_control_ok")
    if a5 == "passed" and a6 == "passed":
        verdict = "reproduced"
    elif a5fx == "passed" and a6fx == "passed" and a8 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": 301,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_301_0830.py"),
        "decks": {
            "P0": [["Island", 56], ["Chaos Wand", 4]],
            "P1": [["Island", 59], ["Lightning Bolt", 1]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
        "setup_line": "P0: 56x Island + 4x Chaos Wand; P1: 59x Island + 1x Lightning Bolt (draw-go)",
        "contract_line": ("Accept: free Bolt cast must finish targeting/resolve for 3 dmg; "
                          "Decline: no cast, cards to bottom"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    await render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close(); RUNLOG.close()
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
