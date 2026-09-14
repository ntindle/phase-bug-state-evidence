#!/usr/bin/env python3
"""Issue #7140: mercurial spelldancer - "Doesn't allow the copy, it does
remove the counters though."

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0, key 'mercurial spelldancer'):
  Mercurial Spelldancer ({1}{U}, 2/1 Phyrexian Rogue):
    "This creature can't be blocked.
     Whenever you cast a noncreature spell, put an oil counter on this
     creature.
     Whenever this creature deals combat damage to a player, you may remove
     two oil counters from it. If you do, when you next cast an instant or
     sorcery spell this turn, copy that spell. You may choose new targets
     for the copy."

Card-data parse state on v0.82.0 (verified 2026-09-13 before the run):
  triggers[0] = SpellCast(noncreature) -> PutCounter oil 1 on SelfRef
  triggers[1] = DamageDone -> RemoveCounter oil 2 on SelfRef, optional,
    sub_ability = CreateDelayedTrigger(WhenNextEvent SpellCast
    [Instant|Sorcery]) -> CopySpell(target=TriggeringSource,
    retarget=MayChooseNewTargets).
  The full clause parses as SUPPORTED. The reported defect is a runtime
  consumption defect: the removal happens but no copy is ever produced.

Reported symptom: after the combat-damage trigger removes two oil counters,
the next instant/sorcery cast that turn is NOT copied.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x mercurial spelldancer, 12x lightning bolt, 18x island, 18x mountain.
  P1: 60x forest (passive punching bag; never plays lands/casts/blocks).

Planned line:
  Turns 1-4: P0 drops lands, casts Spelldancer, casts 2x Lightning Bolt at
    P1 (each noncreature cast => +1 oil counter; P1 20->14).
  Turn 5 (COMBAT1): attack P1 with Spelldancer (unblockable) => 2 combat
    damage (P1 14->12) => DamageDone trigger raises OptionalEffectChoice
    ("you may remove two oil counters"). Driver ACCEPTS. Counters 2->0 and
    the delayed copy trigger is registered.
  Turn 5 post-combat: P0 casts Lightning Bolt at P1. The delayed trigger
    should copy it (MayChooseNewTargets retarget answered with P1).
    Expect P1 12->6 and exactly one bolt CARD in P0's graveyard (the copy
    is not a card and ceases to exist).
  Turn 7 pre-combat: P0 casts Lightning Bolt at ITSELF (+1 oil counter;
    P0 20->17; counters back to 2 without killing P1).
  Turn 7 (COMBAT2): attack P1 with Spelldancer (P1 6->4) => OptionalEffectChoice
    again. Driver DECLINES as the control branch. Counters stay 2.
  Turn 7 post-combat: P0 casts Lightning Bolt at P1 => NO copy is offered
    (decline control): exactly one bolt on the stack, no retarget prompt,
    P1 4->1, bolt card to P0 graveyard.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: RemoveCounter oil 2 + CreateDelayedTrigger /
                      CopySpell MayChooseNewTargets all present (parse check).
  A2_setup            PRE: Spelldancer on P0 BF with exactly 2 oil counters;
                      P1 at 20->14 from the two setup Bolts only.
  A3_removal_accept   OptionalEffectChoice offered on combat damage and
                      accepted; oil counters 2->0.
  A4_copy_created     after the accept-test Bolt is cast, a copy of the Bolt
                      exists on the stack (2 bolt spells) and/or a
                      MayChooseNewTargets retarget prompt is offered and
                      answered. (The reported bug fails HERE: counters removed
                      but no copy.)
  A5_copy_resolves    P1 12->6 from the accept-test Bolt+copy; exactly one
                      bolt card in P0 graveyard from that cast.
  A6_decline_control  combat-2 prompt DECLINED: counters stay 2; decline-test
                      Bolt produces exactly one stack entry and no retarget
                      prompt; P1 4->1.
  A7_cleanup          stack empty, game proceeds after the line completes.

Verdict rule:
  reproduced     iff A3 passes (counters removed on accept) but A4 fails
                 (no copy created / no retarget prompt) - the exact reported
                 symptom.
  not-reproduced iff A3+A4+A5 pass (copy created, retargeted, both resolve).
  blocked        iff A2 fails (setup never reached).

Evidence: evidence/7140/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7140.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-7140")
EVDIR = f"{BACKFILL}/evidence/7140/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DANCER = "mercurial spelldancer"
BOLT = "lightning bolt"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
LANDS = (ISLAND, MOUNTAIN)

P0_DECK = [(DANCER, 12), (BOLT, 12), (ISLAND, 18), (MOUNTAIN, 18)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(started fresh by this run) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key).",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in LANDS):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def counters_of(state, oid):
    """Oil counters on an object; handles dict- or list-shaped counters."""
    o = get_obj(state, oid)
    c = o.get("counters") or {}
    if isinstance(c, dict):
        for k in ("oil", "Oil", "OIL"):
            if k in c:
                v = c[k]
                return int(v.get("count", v) if isinstance(v, dict) else v)
        return sum(int(v.get("count", v)) if isinstance(v, dict) else int(v)
                   for v in c.values())
    if isinstance(c, list):
        n = 0
        for e in c:
            if isinstance(e, dict) and str(e.get("type", "")).lower() == "oil":
                n += int(e.get("count", 1))
        return n
    return 0


def stack_spells(state, name=None):
    out = []
    for e in state.get("stack") or []:
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if name is None or nm == name:
            out.append(e)
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id") \
                    and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)
    return out


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        deep_refs(d, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


def accept_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)

def gy_bolts(state, pid):
    return sum(1 for oid, o in (state.get("objects", {}) or {}).items()
               if o.get("zone") == "Graveyard"
               and str(o.get("base_name") or o.get("name")
                       or "").lower() == BOLT
               and o.get("controller") == pid)


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",          # setup -> combat1 -> accept_test ->
                                   # regen -> combat2 -> decline_test ->
                                   # cleanup -> done
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "prompt_first_seen": {},
        "land_turn": -1,
        "dancer_cast": False,
        "counter_bolts": 0,        # setup bolts cast at P1 (expect 2)
        "attack1_done": False, "attack2_done": False,
        "counters_pre_accept": None, "counters_post_accept": None,
        "accept_offered": False, "accept_done": False,
        "decline_offered": False, "decline_done": False,
        "counters_at_decline": None,
        "test_bolt_cast": False,   # accept-test bolt (turn 5 post-combat)
        "test_bolt_targeted": False,
        "test_bolt_max_stack": 0,  # max simultaneous bolt spells on stack
        "retarget_offered": False, "retarget_done": False,
        "retarget_target": None,
        "p1_life_test_cast": None, "p1_life_after_accept": None,
        "regen_bolt_cast": False,  # turn-7 self bolt (+1 counter)
        "decl_bolt_cast": False,   # decline-test bolt (turn 7 post-combat)
        "decl_bolt_targeted": False,
        "decl_bolt_max_stack": 0,
        "decl_retarget_seen": False,
        "p1_life_decl_cast": None, "p1_life_after_decl": None,
        "p0_life_final": None, "p1_life_final": None,
        "game_code": None,
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "stack_snapshots": [], "prompts": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-dancer")
    p1 = PhaseClient("P1-passive")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            say(f"exported {name}.json "
                f"(turn={(env['state'].get('turn_number'))})")
            return True
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return False

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def vi_choices(opp):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        return (data.get("choices") or data.get("candidates") or [],
                resp.get("type"))

    async def handle_discard(c, pid, tag, st, state, acts):
        # DiscardToHandSize is answered via the viewer interaction
        # (schema/sequence of card choices), not a legacy action.
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            ent = ST["prompt_first_seen"].setdefault(
                iid, {"t0": time.time(), "done": False})
            if ent["done"]:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            # discard lands first, then anything but the dancer
            def rank(ch):
                nm = str(choice_text(ch)).lower()
                if nm in LANDS:
                    return 0
                if nm == DANCER:
                    return 2
                return 1
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: "
                f"{choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    async def mulligan_keep(c, pid, tag, st, state, acts):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def optional_effect(c, pid, tag, st, state):
        """Answer the damage-trigger 'you may remove two oil counters'."""
        if (wf_of(state).get("type") or "") != "OptionalEffectChoice":
            return False
        d = (wf_of(state).get("data") or {})
        if isinstance(d.get("player"), int) and d["player"] != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        opps = vi.get("opportunities", []) or []
        if not opps:
            return False
        for opp in opps:
            iid = opp.get("interactionId")
            ent = ST["prompt_first_seen"].setdefault(
                iid, {"t0": time.time(), "done": False})
            if ent["done"]:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            avals = [accept_of(ch) for ch in chs]
            wire("optional_effect_prompt",
                 {"who": tag, "stage": ST["stage"], "iid": iid,
                  "wf_data": d, "accept_values": avals,
                  "n_choices": len(chs)})
            obs["prompts"].append({"kind": "OptionalEffectChoice",
                                   "who": tag, "stage": ST["stage"],
                                   "accept_values": avals})
            if ST["stage"] == "combat1":
                # pre.json: immediately before the investigated operation
                st_now = st.get("state") or {}
                dancer = bf_ids(st_now, 0, DANCER)
                ST["counters_pre_accept"] = (counters_of(st_now, dancer[0])
                                             if dancer else None)
                say(f"[P0] combat1 OptionalEffectChoice: pre counters="
                    f"{ST['counters_pre_accept']}")
                if not ST["pre_exported"]:
                    if await export_named("pre"):
                        ST["pre_exported"] = True
                ST["accept_offered"] = True
                pick = next((ch for ch in chs
                             if accept_of(ch) == "true"), None)
                if pick is None:
                    say("[P0] ERROR: no accept=true choice; NOT answering")
                    ent["done"] = True
                    return True
                say("[P0] ACCEPTING removal of two oil counters "
                    "(reported branch)")
                await answer_vi(c, opp, pick, tag)
                ST["accept_done"] = True
                ST["stage"] = "accept_test"
                ent["done"] = True
                return True
            elif ST["stage"] == "combat2":
                st_now = st.get("state") or {}
                dancer = bf_ids(st_now, 0, DANCER)
                ST["counters_at_decline"] = (counters_of(st_now, dancer[0])
                                             if dancer else None)
                ST["decline_offered"] = True
                pick = next((ch for ch in chs
                             if accept_of(ch) == "false"), None)
                if pick is None:
                    say("[P0] ERROR: no accept=false choice; NOT answering")
                    ent["done"] = True
                    return True
                say("[P0] DECLINING removal (control branch)")
                await answer_vi(c, opp, pick, tag)
                ST["decline_done"] = True
                ST["stage"] = "decline_test"
                ent["done"] = True
                return True
            else:
                # Unexpected stage for an OptionalEffectChoice: do NOT
                # auto-answer; record it (e.g. a retarget 'may' prompt in
                # accept_test is handled explicitly below instead).
                return False
        return False

    async def target_selection(c, pid, tag, st, state, purpose, seat):
        """Answer a TargetSelection prompt for P0.

        purpose: 'bolt' (choose the bolt's damage target) or
        'copy_retarget' (choose new targets for the copy).
        seat: the player seat to target (1 = P1, 0 = P0/self).
        """
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        d = (wf_of(state).get("data") or {})
        pl = d.get("player")
        if isinstance(pl, int) and pl != pid:
            return False
        if isinstance(pl, dict) and pl.get("player", pid) != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            ent = ST["prompt_first_seen"].setdefault(
                iid, {"t0": time.time(), "done": False})
            if ent["done"]:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            # player candidates carry surfaces[].data.seat (AGENTS.md #6916)
            pick = next((ch for ch in chs if seat_of(ch) == seat), None)
            if pick is None:
                pick = chs[0]
            wire("target_selection",
                 {"who": tag, "purpose": purpose, "iid": iid,
                  "n": len(chs), "wanted_seat": seat,
                  "picked_seat": seat_of(pick),
                  "picked_ref": ref_of(pick),
                  "picked_oid": pick.get("id")})
            obs["prompts"].append({"kind": "TargetSelection", "who": tag,
                                   "purpose": purpose, "wanted_seat": seat,
                                   "picked_seat": seat_of(pick)})
            say(f"[P0] TargetSelection ({purpose}): choosing seat {seat} "
                f"(choice {pick.get('id')})")
            await answer_vi(c, opp, pick, tag)
            # record ACTUALLY submitted oid (AGENTS.md #6906)
            ST["last_target_oid"] = pick.get("id")
            ent["done"] = True
            return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state, acts):
            return
        if await handle_discard(p0, 0, "P0", st, state, acts):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        if await optional_effect(p0, 0, "P0", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # --- target selection routing ---
        # Order matters: a retarget prompt must be claimed BEFORE the
        # generic bolt-targeting branch below.
        if wtype == "TargetSelection":
            stg = ST["stage"]
            if stg == "accept_test" and ST.get("test_bolt_targeted") \
                    and not ST["retarget_done"]:
                # a second TargetSelection after the test bolt's own
                # targeting = the copy's MayChooseNewTargets retarget
                ST["retarget_offered"] = True
                if await target_selection(p0, 0, "P0", st, state,
                                          "copy_retarget", 1):
                    ST["retarget_done"] = True
                    ST["retarget_target"] = ST.get("last_target_oid")
                    return
            if stg == "decline_test" and ST.get("decl_bolt_targeted"):
                ST["decl_retarget_seen"] = True
                obs["unexpected_prompts"].append(
                    {"kind": "TargetSelection-decline-retarget",
                     "stage": stg})
                say("[P0] UNEXPECTED retarget prompt in decline_test; "
                    "answering P1 to keep the game moving")
                if await target_selection(p0, 0, "P0", st, state,
                                          "unexpected_retarget", 1):
                    return
            if stg in ("setup", "accept_test", "regen", "decline_test"):
                # bolt's own target selection; regen bolt targets SELF
                seat = ST.get("pending_bolt_tgt", 1)
                if await target_selection(p0, 0, "P0", st, state,
                                          "bolt", seat):
                    if stg == "accept_test":
                        ST["test_bolt_targeted"] = True
                    elif stg == "decline_test":
                        ST["decl_bolt_targeted"] = True
                    return

        # --- combat declarations ---
        if wtype == "DeclareAttackers" and active == 0:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted("atk", rev):
                dancer = bf_ids(state, 0, DANCER)
                do_atk = False
                if ST["stage"] == "combat1" and dancer \
                        and not ST["attack1_done"] and turn >= 5:
                    do_atk = True
                if ST["stage"] == "combat2" and dancer \
                        and not ST["attack2_done"] and turn >= 7:
                    do_atk = True
                sub = copy.deepcopy(da)
                if do_atk:
                    sub["data"]["attacks"] = [
                        [dancer[0], {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    say(f"[P0] declaring attacker: spelldancer -> P1 "
                        f"(stage={ST['stage']})")
                else:
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                await submit_as_is(p0, sub)
                if do_atk:
                    if ST["stage"] == "combat1":
                        ST["attack1_done"] = True
                    else:
                        ST["attack2_done"] = True
                return

        # --- main-phase actions ---
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop
            if ST["land_turn"] != turn:
                for oid in hand_ids(state, 0):
                    if lname(state, oid) in LANDS:
                        pla = next((a for a in acts
                                    if a.get("type") == "PlayLand"
                                    and (a.get("data") or {}).get(
                                        "object_id") == oid), None)
                        if pla and not acted("land", rev):
                            say(f"[P0] playing land {lname(state, oid)}")
                            await submit_as_is(p0, pla)
                            ST["land_turn"] = turn
                            return
            # cast spelldancer
            dancer_bf = bf_ids(state, 0, DANCER)
            if not dancer_bf and not ST["dancer_cast"]:
                ca = cast_spell_action(acts, state, 0, DANCER)
                if ca and not acted("dancer", rev):
                    say("[P0] casting mercurial spelldancer")
                    await submit_as_is(p0, ca)
                    ST["dancer_cast"] = True
                    return
            # stage-driven bolt casts
            hn = hand_lnames(state, 0)
            stg = ST["stage"]
            want = None
            if stg == "setup" and ST["counter_bolts"] < 2 \
                    and dancer_bf and BOLT in hn:
                want = ("counter", 1)      # setup bolts -> P1
            elif stg == "accept_test" and phase == "PostCombatMain" \
                    and not ST["test_bolt_cast"] and BOLT in hn:
                want = ("test", 1)         # accept-test bolt -> P1
            elif stg == "regen" and not ST["regen_bolt_cast"] \
                    and BOLT in hn and turn >= 7:
                want = ("regen", 0)        # regen bolt -> SELF (P0)
            elif stg == "decline_test" and phase == "PostCombatMain" \
                    and not ST["decl_bolt_cast"] and BOLT in hn:
                want = ("decl", 1)         # decline-test bolt -> P1
            if want:
                kind, tgt = want
                ca = cast_spell_action(acts, state, 0, BOLT)
                if ca and not acted(f"bolt_{kind}", rev):
                    say(f"[P0] casting lightning bolt ({kind})")
                    await submit_as_is(p0, ca)
                    if kind == "counter":
                        ST["counter_bolts"] += 1
                    else:
                        ST[f"{kind}_bolt_cast"] = True
                        ST[f"gy_bolts_pre_{kind}"] = gy_bolts(state, 0)
                        ST[f"p1_life_pre_{kind}"] = life_of(state, 1)
                    ST["pending_bolt_kind"] = kind
                    ST["pending_bolt_tgt"] = tgt
                    return

        # --- stage transitions from live state ---
        dancer_bf = bf_ids(state, 0, DANCER)
        if ST["stage"] == "setup" and dancer_bf:
            c = counters_of(state, dancer_bf[0])
            if c >= 2 and turn >= 5:
                ST["stage"] = "combat1"
                say(f"[P0] stage -> combat1 (counters={c}, turn={turn})")
        if ST["stage"] == "accept_test" and ST["test_bolt_targeted"]:
            # The copy window can close between two 250ms ticks, so the
            # stack is sampled opportunistically while resolution is
            # detected via the graveyard-count delta (robust to a missed
            # stack sample).
            n = len(stack_spells(state, BOLT))
            if n > ST["test_bolt_max_stack"]:
                ST["test_bolt_max_stack"] = n
                snap = [{"id": e.get("id"), "name": e.get("name"),
                         "controller": e.get("controller"),
                         "targets": e.get("targets")}
                        for e in stack_spells(state, BOLT)]
                wire("accept_test_stack", {"n": n, "entries": snap})
                say(f"[P0] accept_test stack: {n} bolt spell(s)")
            if not ST["mid_exported"]:
                # mid.json: the copy window, exported on the first tick
                # after the test bolt's target was chosen
                if await export_named("mid"):
                    ST["mid_exported"] = True
                    if ST["p1_life_test_cast"] is None:
                        ST["p1_life_test_cast"] = \
                            ST.get("p1_life_pre_test")
            gy = gy_bolts(state, 0)
            if n == 0 and gy > ST.get("gy_bolts_pre_test", 0):
                ST["p1_life_after_accept"] = life_of(state, 1)
                ST["gy_bolts_after_accept"] = gy
                say(f"[P0] accept_test bolt resolved: P1 "
                    f"{ST['p1_life_test_cast']}->"
                    f"{ST['p1_life_after_accept']}, P0-gy bolts={gy}")
                ST["stage"] = "regen"
        if ST["stage"] == "regen":
            dancer = bf_ids(state, 0, DANCER)
            c = counters_of(state, dancer[0]) if dancer else 0
            if ST["regen_bolt_cast"] and c >= 2 and turn >= 7:
                ST["stage"] = "combat2"
                say(f"[P0] stage -> combat2 (counters={c}, turn={turn})")
        if ST["stage"] == "decline_test" and ST["decl_bolt_targeted"]:
            n = len(stack_spells(state, BOLT))
            if n > ST["decl_bolt_max_stack"]:
                ST["decl_bolt_max_stack"] = n
                if ST["p1_life_decl_cast"] is None:
                    ST["p1_life_decl_cast"] = ST.get("p1_life_pre_decl")
                say(f"[P0] decline_test stack: {n} bolt spell(s)")
            gy = gy_bolts(state, 0)
            if n == 0 and gy > ST.get("gy_bolts_pre_decl", 0):
                ST["p1_life_decl_cast"] = ST.get("p1_life_pre_decl")
                ST["p1_life_after_decl"] = life_of(state, 1)
                say(f"[P0] decline_test bolt resolved: P1 "
                    f"{ST['p1_life_decl_cast']}->"
                    f"{ST['p1_life_after_decl']}")
                ST["stage"] = "cleanup"
                ST["cleanup_turn"] = turn
        if ST["stage"] == "cleanup":
            if turn > ST.get("cleanup_turn", turn) \
                    and not (state.get("stack") or []):
                ST["p0_life_final"] = life_of(state, 0)
                ST["p1_life_final"] = life_of(state, 1)
                if await export_named("post"):
                    ST["post_exported"] = True
                    ST["stage"] = "done"

        # default: pass priority
        if my_priority(state, 0) and not acted("pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state, acts):
            return
        if await handle_discard(p1, 1, "P1", st, state, acts):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da and not acted(f"p1_{wtype}", rev):
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                else:
                    sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p1, sub)
                return
        # P1 is fully passive otherwise
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 900:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 90 and s:
                last_diag = time.time()
                dancer = bf_ids(s, 0, DANCER)
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"dancer_bf={len(dancer)} "
                    f"counters={[counters_of(s, o) for o in dancer]} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stack_bolts={len(stack_spells(s, BOLT))}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid, post = states.get("pre"), states.get("mid"), states.get("post")

        def dancer_counters(s):
            d = bf_ids(s, 0, DANCER)
            return counters_of(s, d[0]) if d else None

        # ---- A1: parse ----
        try:
            import urllib.request  # noqa
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
            c = cd["mercurial spelldancer"]
            trigs = c.get("triggers", [])
            has_remove = any(
                json.dumps(t).find("RemoveCounter") >= 0 for t in trigs)
            has_delayed = "CreateDelayedTrigger" in json.dumps(trigs)
            has_copy = "CopySpell" in json.dumps(trigs)
            has_retarget = "MayChooseNewTargets" in json.dumps(trigs)
            ok = has_remove and has_delayed and has_copy and has_retarget
            notes.append(f"A1: RemoveCounter={has_remove} "
                         f"CreateDelayedTrigger={has_delayed} "
                         f"CopySpell={has_copy} "
                         f"MayChooseNewTargets={has_retarget}")
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            d = bf_ids(pre, 0, DANCER)
            c = dancer_counters(pre)
            ok = (len(d) == 1 and c == 2 and ST["counter_bolts"] >= 2)
            notes.append(f"A2: dancer_bf={len(d)} counters_pre={c} "
                         f"counter_bolts={ST['counter_bolts']} "
                         f"p1_life_pre={life_of(pre, 1)}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: removal accepted, counters 2->0 ----
        # counters_post_accept measured from mid.json (post-accept window)
        cpost = dancer_counters(mid) if mid is not None else None
        ST["counters_post_accept"] = cpost
        ok = (ST["accept_offered"] and ST["accept_done"]
              and ST["counters_pre_accept"] == 2 and cpost == 0)
        notes.append(f"A3: offered={ST['accept_offered']} "
                     f"done={ST['accept_done']} "
                     f"counters {ST['counters_pre_accept']}->{cpost}")
        ass["A3_removal_accept"] = "passed" if ok else "failed"

        # ---- A4/A5: copy created + resolves ----
        # A5 first (outcome-grounded): the accept-test Bolt must deal 6
        # to P1 (3 original + 3 copy) with exactly one bolt CARD in P0's
        # graveyard from that cast (the copy is not a card).
        dmg_accept = None
        if ST["p1_life_test_cast"] is not None \
                and ST["p1_life_after_accept"] is not None:
            dmg_accept = (ST["p1_life_test_cast"]
                          - ST["p1_life_after_accept"])
            ok = (dmg_accept == 6
                  and ST.get("gy_bolts_after_accept") == 1)
            notes.append(f"A5: P1 {ST['p1_life_test_cast']}->"
                         f"{ST['p1_life_after_accept']} (dmg={dmg_accept}, "
                         f"expect 6); P0-gy bolts="
                         f"{ST.get('gy_bolts_after_accept')} (expect 1)")
        else:
            ok = False
            notes.append("A5 failed: life window not captured")
        ass["A5_copy_resolves"] = "passed" if ok else "failed"

        # A4: copy evidence - a retarget prompt, two bolt spells sampled
        # on the stack, or the 6-damage outcome itself.
        ok = (ST["retarget_offered"]
              or ST["test_bolt_max_stack"] >= 2
              or dmg_accept == 6)
        notes.append(f"A4: retarget_offered={ST['retarget_offered']} "
                     f"retarget_done={ST['retarget_done']} "
                     f"max_bolt_stack={ST['test_bolt_max_stack']} "
                     f"dmg_accept={dmg_accept}")
        ass["A4_copy_created"] = "passed" if ok else "failed"

        # ---- A6: decline control ----
        if post is not None and ST["decline_done"]:
            d = bf_ids(post, 0, DANCER)
            cfin = counters_of(post, d[0]) if d else None
            dmg = ((ST["p1_life_decl_cast"] or 0)
                   - (ST["p1_life_after_decl"] or 0)) \
                if ST["p1_life_decl_cast"] is not None \
                and ST["p1_life_after_decl"] is not None else None
            ok = (ST["counters_at_decline"] == 2
                  and ST["decl_bolt_max_stack"] == 1
                  and not ST["decl_retarget_seen"]
                  and dmg == 3)
            notes.append(f"A6: counters_at_decline="
                         f"{ST['counters_at_decline']} counters_post={cfin} "
                         f"decl_max_stack={ST['decl_bolt_max_stack']} "
                         f"decl_retarget_seen={ST['decl_retarget_seen']} "
                         f"P1 {ST['p1_life_decl_cast']}->"
                         f"{ST['p1_life_after_decl']} (dmg={dmg}, expect 3)")
        else:
            ok = False
            notes.append(f"A6 failed: decline_done={ST['decline_done']} "
                         f"post={'ok' if post is not None else 'missing'}")
        ass["A6_decline_control"] = "passed" if ok else "failed"

        # ---- A7: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty and wft in ("Priority",)
            notes.append(f"A7: stack_empty={stack_empty} post_wf={wft} "
                         f"life_final={ST['p0_life_final']}/"
                         f"{ST['p1_life_final']}")
        else:
            ok = False
            notes.append("A7 failed: post.json missing")
        ass["A7_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif (ass["A3_removal_accept"] == "passed"
                and ass["A4_copy_created"] == "failed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: counters were removed on "
                         "accept but no copy of the next instant/sorcery "
                         "was created - the exact reported symptom")
        elif (ass["A3_removal_accept"] == "passed"
                and ass["A4_copy_created"] == "passed"
                and ass["A5_copy_resolves"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: accept removed counters, "
                         "copy was created, retargeted, and both bolt+copy "
                         "resolved for 6")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7140,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7140.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7140.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Mercurial Spelldancer / 12x Lightning Bolt density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "P1 is a fully passive punching bag (60x Forest, never "
                "plays lands, never casts, never blocks).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7140.py",
                    f"{EVDIR}/scenario_7140.py")
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{RUN_ID}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            gc = ST.get("game_code") or ""
            excerpt = [ln for ln in clean.splitlines()
                       if gc and gc in ln]
            if not excerpt:
                excerpt = clean.splitlines()[-400:]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 980
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7140 - Mercurial Spelldancer",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-13 - "
               "delayed copy trigger after counter removal",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: combat damage -> may remove two oil "
               "counters. If you do, copy your next instant/sorcery.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: RemoveCounter + CopySpell parse ok",
            "A2_setup": "PRE: dancer on BF, exactly 2 oil counters",
            "A3_removal_accept": "accept: counters 2->0",
            "A4_copy_created": "copy of next bolt on stack (2 bolt spells)",
            "A5_copy_resolves": "P1 12->6; one bolt card in P0 gy",
            "A6_decline_control": "decline: counters stay 2, no copy, P1 4->1",
            "A7_cleanup": "stack empty; game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((40, y), f"{k}: {v}", fill=col)
            d.text((260, y), lab, fill=(180, 190, 205))
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {})
        lines = [
            f"accept: offered={ds.get('accept_offered')} "
            f"done={ds.get('accept_done')} "
            f"counters {ds.get('counters_pre_accept')}->"
            f"{ds.get('counters_post_accept')}",
            f"copy: retarget_offered={ds.get('retarget_offered')} "
            f"retarget_done={ds.get('retarget_done')} "
            f"max_bolt_stack={ds.get('test_bolt_max_stack')}",
            f"life: P1 test-cast {ds.get('p1_life_test_cast')}->"
            f"{ds.get('p1_life_after_accept')}; "
            f"decline {ds.get('p1_life_decl_cast')}->"
            f"{ds.get('p1_life_after_decl')}",
            f"decline: offered={ds.get('decline_offered')} "
            f"done={ds.get('decline_done')} "
            f"counters_at_decline={ds.get('counters_at_decline')} "
            f"decl_max_stack={ds.get('decl_bolt_max_stack')}",
            f"final life P0/P1: {ds.get('p0_life_final')}/"
            f"{ds.get('p1_life_final')}",
        ]
        for ln in lines:
            d.text((40, y), ln[:118], fill=(160, 175, 195))
            y += 24
        y += 8
        d.text((24, y), "Notes:", fill=(200, 210, 225))
        y += 24
        for n in run.get("notes", [])[:14]:
            d.text((40, y), ("- " + n)[:116], fill=(150, 165, 185))
            y += 22
        img.save(f"{EVDIR}/summary.png")
        say("rendered summary.png")

    def write_manifest():
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_7140.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        # write twice: scenario_run.log is hashed LAST, after all say()
        # logging is done (no say() may follow the second write).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    await finish()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
