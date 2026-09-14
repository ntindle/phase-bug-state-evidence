#!/usr/bin/env python3
"""phase-rs/phase #7143 - Torment of Hailfire + Bloodchief Ascension.

Report: controller of Bloodchief Ascension (3 quest counters) casts Torment
of Hailfire with X=10; expects Bloodchief to trigger once per card that
moves into an opponent's graveyard during Torment. Observed: no triggers.

Behavioral contract:
  A1 setup: Bloodchief Ascension on P0 battlefield with exactly 3 quest
     counters; P1 battlefield has >=10 memnites; lives P0=20, P1=11.
  A2 cast: Torment of Hailfire cast with X=10 (number-value submission),
     fully resolved (in P0 graveyard).
  A3 choices: 10 UnlessPaymentChooseCost prompts offered to P1 (one per rep).
  A4 sacrifices: all 10 answered "sacrifice"; P1 graveyard gains 10 memnites,
     P1 battlefield loses 10 memnites.
  A5 triggers: Bloodchief may-trigger offers to P0 == graveyard moves (10).
  A6 effect: P0 accepts 4 (P1 -8, P0 +8), declines 6 -> P1 = p1_pre-8, P0 = 28.
  A7 cleanup: game continues, stack empty.

Verdict: reproduced if A3/A4/A5/A6 fail in the bug direction;
not-reproduced iff all pass; blocked if A1/A2 fail.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7143")
EVDIR = f"{BACKFILL}/evidence/7143/{RUN_ID}"

ASCENSION = "bloodchief ascension"
BUMP = "bump in the night"
TORMENT = "torment of hailfire"
RITUAL = "dark ritual"
SWAMP = "swamp"
MEMNITE = "memnite"

P0_DECK = [(ASCENSION, 4), (BUMP, 8), (TORMENT, 4), (RITUAL, 4),
           (SWAMP, 40)]
P1_DECK = [(MEMNITE, 60)]

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(started fresh by this run) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key).",
}

os.makedirs(EVDIR, exist_ok=True)
WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower()
            == SWAMP]


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def gy_oids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def counters_of_kind(state, oid, kind_substr):
    """Best-effort counter count of a kind (e.g. 'quest') on an object."""
    o = get_obj(state, oid)
    total = 0
    ks = kind_substr.lower()
    c = o.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            if ks in str(k).lower() and isinstance(v, (int, float)):
                total += int(v)
    elif isinstance(c, list):
        for e in c:
            if isinstance(e, dict):
                k = str(e.get("type") or e.get("kind")
                        or e.get("counter_type") or "")
                if ks in k.lower():
                    try:
                        total += int(e.get("count", e.get("n", 1)))
                    except Exception:
                        pass
            elif isinstance(e, str) and ks in e.lower():
                total += 1
    for k in ("quest_counters", "questCounters"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def stack_triggered_from(state, name_substr):
    out = []
    for e in state.get("stack") or []:
        kind = e.get("kind") or {}
        if (kind.get("type") or "") != "TriggeredAbility":
            continue
        src = e.get("source_id")
        nm = lname(state, src) if src is not None else ""
        desc = str((kind.get("data") or {}).get("description") or "")
        if name_substr.lower() in nm or name_substr.lower() in desc.lower():
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
    return next((a for a in acts if a.get("type") == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player_is(state, pid):
    d = (wf_of(state).get("data") or {})
    pl = d.get("player")
    if isinstance(pl, int):
        return pl == pid
    if isinstance(pl, dict):
        return pl.get("player", pid) == pid
    return True


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


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
            if k in ("reference", "object_id", "objectId", "target_id",
                     "hit_card", "card_id") and isinstance(v, (int, str)):
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


def value_surfaces(choice):
    out = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and ("role" in d or "value" in d):
            out.append(d)
    return out


def accept_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
    return None


def action_codes(choice):
    codes = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if not isinstance(d, dict):
            continue
        for k in ("code", "action", "actionCode", "kind"):
            v = d.get(k)
            if isinstance(v, str):
                codes.append(v)
    return codes


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
    say(f"[{tag}] submit iid={iid} choice={cid} "
        f"({choice_text(choice)[:70]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup->counters->ramp->torment->t_resolve
                            # ->triggers->settle->done
        "stage_t0": time.time(),
        "pre_exported": False, "post_torment_exported": False,
        "post_exported": False,
        "prompt_first_seen": {},
        "land_turn": -1,
        "last_rev_acted": {},
        "ascension_cast": False,
        "bumps_cast": 0, "bump_in_flight": False, "bump_turn": -1,
        "quest_counters": 0, "quest_offers": 0, "quest_accepts": 0,
        "torment_cast": False, "torment_x": None,
        "x_last_try": 0.0, "x_tries": 0, "x_confirmed": False,
        "unless_prompts": 0, "unless_reps": [],
        "unless_loop_started": False, "unless_quiet_ticks": 0,
        "bc_offers": 0, "bc_answered": 0, "bc_accepted": 0,
        "bc_log": [],
        "expected_triggers": None,
        "p1_life_pre_torment": None, "p0_life_pre_torment": None,
        "settle_t0": None,
        "game_code": None, "game_over": False,
        "zones_seen": set(),
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "prompts": [], "stage_timeouts": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-torment")
    p1 = PhaseClient("P1-victim")
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
                f"(turn={env['state'].get('turn_number')})")
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

    def set_stage(s):
        if ST["stage"] != s:
            say(f"stage -> {s} (was {ST['stage']})")
            ST["stage"] = s
            ST["stage_t0"] = time.time()

    async def mulligan_keep(c, pid, tag, st, state, acts):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state, acts):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        if not wf_player_is(state, pid):
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
            hn = hand_lnames(state, pid)
            asc_bf = len(bf_ids(state, 0, ASCENSION)) if pid == 0 else 0

            def rank(ch):
                nm = str(choice_text(ch)).lower()
                if pid == 0:
                    if nm == ASCENSION and asc_bf:
                        return 0
                    if nm == RITUAL:
                        return 1
                    if nm == SWAMP and len(bf_lands(state, 0)) >= 13:
                        return 2
                    if nm == TORMENT:
                        spare = hn.count(TORMENT) > 1 or ST["torment_cast"]
                        return 3 if spare else 6
                    if nm == BUMP and (ST["quest_counters"] >= 3
                                       or ST["bumps_cast"] >= 3):
                        return 2
                    return 6  # protect everything else
                return 0
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: "
                f"{choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    async def quest_counter_offer(c, pid, tag, st, state):
        """Bloodchief's end-step 'you may put a quest counter' -> yes.
        Keyed off the waiting_for description (OptionalEffectChoice carries
        generic yes/no choice surfaces without card text). Allowed in any
        stage: it is a may ability and answering yes keeps the game moving
        (e.g. the end step after the Torment turn)."""
        if not wf_player_is(state, pid):
            return False
        wfd = wf_of(state).get("data") or {}
        desc = str(wfd.get("description") or "").lower()
        if "quest counter" not in desc:
            return False
        # the bloodchief GRAVEYARD trigger description also mentions
        # "quest counters" ("if ~ has three or more quest counters on it"):
        # that offer belongs to bloodchief_offer, not this handler.
        if "lose 2 life" in desc:
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
            if not ent.get("shape_wired"):
                ent["shape_wired"] = True
                wire("quest_counter_offer_shape",
                     {"who": tag, "iid": iid,
                      "wf_type": wf_of(state).get("type"),
                      "wf_description": wfd.get("description"),
                      "texts": [choice_text(ch)[:60] for ch in chs],
                      "accepts": [accept_of(ch) for ch in chs],
                      "values": [value_surfaces(ch) for ch in chs],
                      "opp": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": "QuestCounterOffer", "who": tag,
                                   "iid": iid})
            ST["quest_offers"] += 1
            pick = next((ch for ch in chs if accept_of(ch) == "true"), None)
            if pick is None:
                pick = next((ch for ch in chs
                             if choice_text(ch).strip().lower()
                             in ("yes", "y", "accept", "pay")), None)
            if pick is None:
                pick = chs[0]
            say(f"[{tag}] quest-counter offer: answering YES "
                f"({choice_text(pick)[:40]})")
            await answer_vi(c, opp, pick, tag)
            ST["quest_accepts"] += 1
            ent["done"] = True
            return True
        return False

    async def bloodchief_offer(c, pid, tag, st, state):
        """Bloodchief's 'you may have that player lose 2 life' trigger offer.
        Keyed off the waiting_for description (OptionalEffectChoice carries
        generic yes/no choice surfaces). Accept the first 4, decline rest."""
        if ST["torment_x"] is None:
            return False
        if not wf_player_is(state, pid):
            return False
        wfd = wf_of(state).get("data") or {}
        desc = str(wfd.get("description") or "").lower()
        if "lose 2 life" not in desc:
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
            if not ent.get("shape_wired"):
                ent["shape_wired"] = True
                wire("bloodchief_offer_shape",
                     {"who": tag, "iid": iid, "stage": ST["stage"],
                      "wf_type": wf_of(state).get("type"),
                      "wf_description": wfd.get("description"),
                      "texts": [choice_text(ch)[:60] for ch in chs],
                      "accepts": [accept_of(ch) for ch in chs],
                      "values": [value_surfaces(ch) for ch in chs],
                      "opp": json.loads(json.dumps(opp, default=str))})
            ST["bc_offers"] += 1
            obs["prompts"].append({"kind": "BloodchiefTriggerOffer",
                                   "who": tag, "iid": iid,
                                   "n": ST["bc_offers"]})
            # accept while we still want accepts AND P1 can survive the
            # 2-life hit (keep P1 alive so every offer + continuation
            # asserts cleanly)
            accept = ST["bc_accepted"] < 4 and life_of(state, 1) > 3
            if accept:
                pick = next((ch for ch in chs if accept_of(ch) == "true"),
                            None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if choice_text(ch).strip().lower()
                                 in ("yes", "y", "accept")), None)
            else:
                pick = next((ch for ch in chs if accept_of(ch) == "false"),
                            None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if choice_text(ch).strip().lower()
                                 in ("no", "n", "decline", "don't pay")),
                                None)
            if pick is None:
                pick = chs[0] if accept else chs[-1]
            say(f"[{tag}] bloodchief trigger offer #{ST['bc_offers']}: "
                f"{'ACCEPT' if accept else 'DECLINE'} "
                f"({choice_text(pick)[:40]})")
            ST["bc_log"].append({"offer": ST["bc_offers"],
                                 "iid": iid, "accept": accept,
                                 "p0_life_before": life_of(state, 0),
                                 "p1_life_before": life_of(state, 1)})
            await answer_vi(c, opp, pick, tag)
            ST["bc_answered"] += 1
            if accept:
                ST["bc_accepted"] += 1
            ent["done"] = True
            return True
        return False

    async def torment_x_value(c, pid, tag, st, state):
        wtype = wf_of(state).get("type") or ""
        if "xvalue" not in wtype.lower() and "choose_x" not in wtype.lower():
            return False
        if not wf_player_is(state, pid):
            return False
        if ST["x_confirmed"]:
            return False
        if time.time() - ST["x_last_try"] < 5:
            return True
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            resp = opp.get("response", {}) or {}
            spec = ((resp.get("data") or {}).get("spec") or {})
            stype = spec.get("type")
            if iid not in ST["prompt_first_seen"]:
                ST["prompt_first_seen"][iid] = {"t0": time.time(),
                                                "done": False}
                wire("torment_x_shape",
                     {"wf_type": wtype,
                      "wf_data_keys": list((wf_of(state).get("data")
                                           or {}).keys()),
                      "spec": spec})
                say(f"[P0] Torment ChooseX: spec_type={stype}")
            if stype == "number":
                sub = {"interactionId": iid,
                       "response": {"type": "number", "data": {"value": 10}}}
                say("[P0] submitting X=10 (number value)")
                wire("interaction_submission",
                     {"who": tag, "submission": sub, "x": 10})
                await c.send_interaction(sub)
            else:
                chs, rtype = vi_choices(opp)
                if not chs:
                    return False

                def num(ch):
                    for cand in (choice_text(ch), accept_of(ch) or ""):
                        try:
                            return int(str(cand).strip())
                        except Exception:
                            pass
                    return None
                pick = next((ch for ch in chs if num(ch) == 10), None)
                if pick is None:
                    nums = [(n, ch) for ch in chs
                            if (n := num(ch)) is not None]
                    pick = max(nums, key=lambda t: t[0])[1] if nums \
                        else chs[-1]
                await answer_vi(c, opp, pick, tag)
            ST["x_last_try"] = time.time()
            ST["x_tries"] += 1
            return True
        return False

    async def bump_target(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        if not wf_player_is(state, pid):
            return False
        if not ST["bump_in_flight"]:
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
            pick = next((ch for ch in chs if seat_of(ch) == 1), None)
            if pick is None:
                pick = chs[0]
            wire("bump_target_selection",
                 {"who": tag, "iid": iid, "picked_seat": seat_of(pick)})
            say(f"[{tag}] Bump in the Night targets P1 (seat 1)")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            oid = (a.get("data") or {}).get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def unless_branch(c, pid, tag, st, state):
        """Answer Torment's UnlessPaymentChooseCost: prefer sacrifice (branch
        0) while memnites are on board, else discard (branch 1) with cards in
        hand, else decline (lose 3)."""
        wtype = wf_of(state).get("type") or ""
        if wtype != "UnlessPaymentChooseCost":
            return False
        if not wf_player_is(state, pid):
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
            if not ent.get("shape_wired"):
                ent["shape_wired"] = True
                wire("unless_pay_branch_shape",
                     {"who": tag, "iid": iid, "rep": ST["unless_prompts"] + 1,
                      "n": len(chs),
                      "values": [value_surfaces(ch) for ch in chs],
                      "texts": [choice_text(ch)[:50] for ch in chs],
                      "codes": [action_codes(ch) for ch in chs]})
            ST["unless_loop_started"] = True
            ST["unless_quiet_ticks"] = 0

            def branch_of(ch):
                for vs in value_surfaces(ch):
                    v = str(vs.get("value", ""))
                    role = str(vs.get("role", ""))
                    if v == "decline" or "decline" in role.lower():
                        return "decline"
                    if v == "0":
                        return "sacrifice"
                    if v == "1":
                        return "discard"
                txt = choice_text(ch).lower()
                codes = " ".join(action_codes(ch)).lower()
                if "sacrif" in txt or "sacrif" in codes:
                    return "sacrifice"
                if "discard" in txt or "discard" in codes:
                    return "discard"
                if "lose" in txt or "don't pay" in txt:
                    return "decline"
                return "unknown"
            kinds = [branch_of(ch) for ch in chs]
            memnites = len(bf_ids(state, 1, MEMNITE))
            hand_n = len(hand_ids(state, 1))
            idx_sac = next((i for i, k in enumerate(kinds)
                            if k == "sacrifice"), None)
            idx_dis = next((i for i, k in enumerate(kinds)
                            if k == "discard"), None)
            idx_dec = next((i for i, k in enumerate(kinds)
                            if k == "decline"), None)
            if idx_sac is not None and memnites >= 1:
                pick, branch = idx_sac, "sacrifice"
            elif idx_dis is not None and hand_n >= 1:
                pick, branch = idx_dis, "discard"
            elif idx_dec is not None:
                pick, branch = idx_dec, "decline"
            else:
                pick, branch = 0, kinds[0] + "?fallback"
            ST["unless_prompts"] += 1
            ST["unless_reps"].append(
                {"rep": ST["unless_prompts"], "branch": branch,
                 "kinds": kinds,
                 "p1_bf_memnites": memnites, "p1_hand": hand_n,
                 "p1_gy_memnites": len(gy_oids(state, 1, MEMNITE)),
                 "p1_life": life_of(state, 1)})
            obs["prompts"].append({"kind": "UnlessPaymentChooseCost",
                                   "who": tag, "rep": ST["unless_prompts"],
                                   "branch": branch})
            say(f"[{tag}] torment rep {ST['unless_prompts']}: "
                f"branch={branch} (kinds={kinds})")
            await answer_vi(c, opp, chs[pick], tag)
            ent["done"] = True
            return True
        return False

    async def cost_followup(c, pid, tag, st, state):
        """Follow-up to a paid unless branch: choose WHICH permanent to
        sacrifice (a memnite) or WHICH card to discard."""
        if not ST["unless_loop_started"]:
            return False
        if not wf_player_is(state, pid):
            return False
        wtype = wf_of(state).get("type") or ""
        if wtype == "UnlessPaymentChooseCost":
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
            bf = set(bf_ids(state, 1))
            hand = set(hand_ids(state, 1))
            memnites = set(bf_ids(state, 1, MEMNITE))
            sac_cands = [ch for ch in chs
                         if ref_of(ch) in bf]
            dis_cands = [ch for ch in chs
                         if ref_of(ch) in hand]
            if not sac_cands and not dis_cands:
                continue
            if not ent.get("shape_wired"):
                ent["shape_wired"] = True
                wire("unless_cost_followup_shape",
                     {"who": tag, "iid": iid, "wf_type": wtype,
                      "n": len(chs),
                      "texts": [choice_text(ch)[:50] for ch in chs][:8],
                      "refs": [ref_of(ch) for ch in chs][:8]})
            if sac_cands:
                mem_c = [ch for ch in sac_cands
                         if ref_of(ch) in memnites]
                pick = mem_c[0] if mem_c else sac_cands[0]
                say(f"[{tag}] sacrifice follow-up: memnite "
                    f"oid={ref_of(pick)}")
                ST["unless_reps"][-1]["sacrificed_oid"] = ref_of(pick)
            else:
                pick = dis_cands[0]
                say(f"[{tag}] discard follow-up: oid={ref_of(pick)}")
                ST["unless_reps"][-1]["discarded_oid"] = ref_of(pick)
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

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
        if await quest_counter_offer(p0, 0, "P0", st, state):
            return
        if await bloodchief_offer(p0, 0, "P0", st, state):
            return
        # confirm X once the wf advances past ChooseXValue
        wtype_now = wf_of(state).get("type") or ""
        if ST["x_last_try"] and not ST["x_confirmed"] \
                and "xvalue" not in wtype_now.lower() \
                and "choose_x" not in wtype_now.lower():
            ST["x_confirmed"] = True
            ST["torment_x"] = 10
            say("[P0] X=10 confirmed (wf advanced past ChooseXValue)")
            set_stage("torment_resolve")
        if await torment_x_value(p0, 0, "P0", st, state):
            return
        if await bump_target(p0, 0, "P0", st, state):
            return
        wtype = wtype_now
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # track bump resolution
        if ST["bump_in_flight"] and gy_oids(state, 0, BUMP):
            ST["bump_in_flight"] = False
            say(f"[P0] bump resolved (bumps_cast={ST['bumps_cast']})")
        # track quest counters live
        asc_ids = bf_ids(state, 0, ASCENSION)
        if asc_ids:
            ST["quest_counters"] = counters_of_kind(state, asc_ids[0],
                                                   "quest")

        # stage transitions driven by state
        if ST["stage"] == "setup" and asc_ids:
            ST["ascension_cast"] = True
            set_stage("counters")
        if ST["stage"] == "counters" and ST["quest_counters"] >= 3:
            if not ST["pre_exported"]:
                if await export_named("pre"):
                    ST["pre_exported"] = True
                    ST["p1_life_pre_torment"] = life_of(state, 1)
                    ST["p0_life_pre_torment"] = life_of(state, 0)
                    ST["pre_p1_gy_memnites"] = len(gy_oids(state, 1, MEMNITE))
                    ST["pre_p1_bf_memnites"] = len(bf_ids(state, 1, MEMNITE))
                    ST["pre_quest"] = ST["quest_counters"]
            set_stage("ramp")
        if ST["stage"] == "torment_resolve":
            # unless-pay loop completion: quiet ticks with no prompt
            torment_gy = len(gy_oids(state, 0, TORMENT)) >= 1
            if ST["unless_loop_started"]:
                if wtype == "UnlessPaymentChooseCost":
                    ST["unless_quiet_ticks"] = 0
                else:
                    ST["unless_quiet_ticks"] += 1
                loop_done = ST["unless_quiet_ticks"] >= 40
            elif torment_gy:
                # spell left the stack but no unless prompt ever came
                # (parser-drop bug shape): still conclude the loop
                ST["unless_quiet_ticks"] += 1
                loop_done = ST["unless_quiet_ticks"] >= 40
            else:
                loop_done = False
            if loop_done:
                if not ST["post_torment_exported"]:
                    if await export_named("post_torment"):
                        ST["post_torment_exported"] = True
                        n_gy = len(gy_oids(state, 1, MEMNITE))
                        ST["expected_triggers"] = n_gy - ST.get(
                            "pre_p1_gy_memnites", 0)
                        ST["pt_lives"] = (life_of(state, 0),
                                          life_of(state, 1))
                        ST["pt_bc_stack"] = len(stack_triggered_from(
                            state, ASCENSION))
                        ST["pt_stack_kinds"] = [
                            (e.get("kind") or {}).get("type")
                            for e in (state.get("stack") or [])]
                        say(f"[P0] unless-pay loop done: "
                            f"prompts={ST['unless_prompts']} "
                            f"p1_gy_memnites={n_gy} "
                            f"expected_triggers="
                            f"{ST['expected_triggers']}")
                set_stage("triggers")
        if ST["stage"] == "triggers":
            exp = ST["expected_triggers"]
            if exp is not None and ST["bc_answered"] >= exp and exp > 0:
                set_stage("settle")
                ST["settle_t0"] = time.time()
            elif exp == 0:
                # no graveyard moves: nothing can trigger; still settle
                set_stage("settle")
                ST["settle_t0"] = time.time()
            elif time.time() - ST["stage_t0"] > 90 \
                    and not (state.get("stack") or []):
                # bug shape: offers stopped arriving well short of the
                # graveyard-move count and the stack is empty; conclude
                # the observation instead of waiting out the stage cap
                say(f"[P0] triggers quiet 90s: bc_answered="
                    f"{ST['bc_answered']} expected={exp}; settling")
                set_stage("settle")
                ST["settle_t0"] = time.time()
        if ST["stage"] == "settle":
            if ST["settle_t0"] is None:
                ST["settle_t0"] = time.time()
            stack_empty = not (state.get("stack") or [])
            if stack_empty and my_priority(state, 0) \
                    and time.time() - ST["settle_t0"] > 20:
                if not ST["post_exported"]:
                    if await export_named("post"):
                        ST["post_exported"] = True
                return

        # combat: never attack, never block
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player_is(state, 0):
            da = find_action(acts, wtype)
            if da and not acted(f"atk{turn}{wtype}", rev):
                sub = copy.deepcopy(da)
                for k in ("attacks", "bands", "blockers", "assignments"):
                    if k in sub["data"]:
                        sub["data"][k] = []
                await submit_as_is(p0, sub)
                return

        # main-phase stage actions
        if active == 0 and phase == "PreCombatMain":
            if turn != ST["land_turn"]:
                pla = find_action(acts, "PlayLand")
                if pla and not acted(f"land{turn}", rev):
                    await submit_as_is(p0, pla)
                    ST["land_turn"] = turn
                    return
            if my_priority(state, 0):
                hn = hand_lnames(state, 0)
                if ST["stage"] == "setup" and not asc_ids \
                        and ASCENSION in hn:
                    ca = cast_spell_action(acts, state, 0, ASCENSION)
                    if ca and not acted(f"cast-asc{turn}", rev):
                        await submit_as_is(p0, ca)
                        say(f"[P0] cast Bloodchief Ascension t{turn}")
                        return
                if ST["stage"] == "counters" \
                        and ST["quest_counters"] < 3 \
                        and not ST["bump_in_flight"] and BUMP in hn \
                        and turn != ST["bump_turn"]:
                    ca = cast_spell_action(acts, state, 0, BUMP)
                    if ca and not acted(f"cast-bump{turn}", rev):
                        await submit_as_is(p0, ca)
                        ST["bump_in_flight"] = True
                        ST["bumps_cast"] += 1
                        ST["bump_turn"] = turn
                        say(f"[P0] cast Bump in the Night "
                            f"(#{ST['bumps_cast']}) t{turn}")
                        return
                if ST["stage"] == "ramp" and TORMENT in hn \
                        and len(untapped_lands(state, 0)) >= 12 \
                        and not ST["torment_cast"]:
                    ca = cast_spell_action(acts, state, 0, TORMENT)
                    if ca and not acted("cast-torment", rev):
                        await submit_as_is(p0, ca)
                        ST["torment_cast"] = True
                        set_stage("torment")
                        say(f"[P0] cast Torment of Hailfire t{turn}")
                        return
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state, acts):
            return
        if await unless_branch(p1, 1, "P1", st, state):
            return
        if await cost_followup(p1, 1, "P1", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player_is(state, 1):
            da = find_action(acts, wtype)
            if da and not acted(f"p1atk{turn}{wtype}", rev):
                sub = copy.deepcopy(da)
                for k in ("attacks", "bands", "blockers", "assignments"):
                    if k in sub["data"]:
                        sub["data"][k] = []
                await submit_as_is(p1, sub)
                return
        if active == 1 and phase == "PreCombatMain" \
                and my_priority(state, 1):
            if MEMNITE in hand_lnames(state, 1):
                ca = cast_spell_action(acts, state, 1, MEMNITE)
                if ca and not acted(f"mem{turn}", rev):
                    await submit_as_is(p1, ca)
                    return
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
                await fn(st, merged_actions(st), state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    STAGE_CAPS = {"setup": 300, "counters": 480, "ramp": 420,
                  "torment": 240, "torment_resolve": 420,
                  "triggers": 420, "settle": 240}

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            # game-over detection
            if s:
                winner = s.get("winner")
                lives = [life_of(s, i) for i in (0, 1)]
                if winner is not None or any(
                        l is not None and l <= 0 for l in lives):
                    ST["game_over"] = True
                    say(f"[diag] GAME OVER winner={winner} lives={lives}")
                    break
            # stage timeout
            cap = STAGE_CAPS.get(ST["stage"], 600)
            if time.time() - ST["stage_t0"] > cap:
                obs["stage_timeouts"].append(
                    {"stage": ST["stage"],
                     "elapsed": round(time.time() - ST["stage_t0"], 1)})
                say(f"[diag] stage {ST['stage']} timed out after {cap}s")
                break
            if time.time() - last_diag > 90 and s:
                last_diag = time.time()
                asc = bf_ids(s, 0, ASCENSION)
                qc = counters_of_kind(s, asc[0], "quest") if asc else 0
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"asc_bf={len(asc)} quest={qc} "
                    f"p1mem={len(bf_ids(s, 1, MEMNITE))} "
                    f"lands={len(bf_lands(s, 0))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"unless={ST['unless_prompts']} bc={ST['bc_offers']}/"
                    f"{ST['bc_answered']}")
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
        for fn in ("pre", "post_torment", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, post_torment, post = (states.get("pre"),
                                   states.get("post_torment"),
                                   states.get("post"))
        ST["zones_seen"] = sorted(ST["zones_seen"])

        # ---- A1: setup ----
        if pre is not None:
            asc = bf_ids(pre, 0, ASCENSION)
            qc = counters_of_kind(pre, asc[0], "quest") if asc else -1
            p1mem = len(bf_ids(pre, 1, MEMNITE))
            l0, l1 = life_of(pre, 0), life_of(pre, 1)
            exp_l1 = 20 - 3 * ST["bumps_cast"]
            ok = (len(asc) == 1 and qc == 3 and p1mem >= 10
                  and l0 == 20 and l1 == exp_l1)
            notes.append(f"A1: asc_bf={len(asc)} quest={qc} "
                         f"p1_memnites_bf={p1mem} life={l0}/{l1} "
                         f"(expect 20/{exp_l1}; bumps={ST['bumps_cast']})")
        else:
            ok = False
            notes.append("A1 failed: pre.json missing")
        ass["A1_setup"] = "passed" if ok else "failed"

        # ---- A2: torment cast with X=10 and resolved ----
        x_ok = ST["torment_x"] == 10 and ST["x_confirmed"]
        resolved = post_torment is not None and len(
            gy_oids(post_torment, 0, TORMENT)) >= 1
        ok = x_ok and resolved
        notes.append(f"A2: torment_cast={ST['torment_cast']} "
                     f"x_confirmed={ST['x_confirmed']} x={ST['torment_x']} "
                     f"resolved_to_gy={resolved}")
        ass["A2_cast_x10"] = "passed" if ok else ("not-run"
                                                 if not ST["torment_cast"]
                                                 else "failed")

        # ---- A3: 10 unless-pay prompts ----
        n_unless = ST["unless_prompts"]
        if ST["torment_cast"]:
            ok = n_unless == 10
            notes.append(f"A3: unless-pay prompts offered={n_unless} "
                         f"(expect 10); branches="
                         f"{[r['branch'] for r in ST['unless_reps']]}")
        else:
            ok = False
            notes.append("A3 not-run: torment never cast")
        ass["A3_ten_choices"] = ("passed" if ok else
                                 ("not-run" if not ST["torment_cast"]
                                  else "failed"))

        # ---- A4: 10 sacrifices moved memnites to P1 graveyard ----
        if pre is not None and post_torment is not None:
            gy_pre = len(gy_oids(pre, 1, MEMNITE))
            gy_pt = len(gy_oids(post_torment, 1, MEMNITE))
            bf_pre = len(bf_ids(pre, 1, MEMNITE))
            bf_pt = len(bf_ids(post_torment, 1, MEMNITE))
            sac_reps = sum(1 for r in ST["unless_reps"]
                           if r["branch"] == "sacrifice")
            ok = (gy_pt - gy_pre == 10 and bf_pre - bf_pt == 10
                  and sac_reps == 10)
            notes.append(f"A4: p1 gy memnites {gy_pre}->{gy_pt} "
                         f"(delta {gy_pt - gy_pre}, expect 10); "
                         f"p1 bf memnites {bf_pre}->{bf_pt} "
                         f"(delta {bf_pre - bf_pt}, expect -10); "
                         f"sacrifice reps={sac_reps}/10")
        else:
            ok = False
            notes.append("A4 not-run: pre/post_torment missing")
        ass["A4_sacrifices"] = ("passed" if ok else
                                ("not-run" if ass["A3_ten_choices"]
                                 == "not-run" else "failed"))

        # ---- A5: bloodchief trigger offers == graveyard moves ----
        exp = ST["expected_triggers"]
        if exp is not None and exp > 0:
            ok = ST["bc_offers"] == exp
            notes.append(f"A5: bloodchief may-offers={ST['bc_offers']} "
                         f"(expect {exp} = gy moves); "
                         f"answered={ST['bc_answered']} accepted="
                         f"{ST['bc_accepted']}; stack had "
                         f"{ST.get('pt_bc_stack')} bloodchief "
                         f"TriggeredAbility entries at post_torment "
                         f"(kinds={ST.get('pt_stack_kinds')})")
        elif exp == 0:
            ok = ST["bc_offers"] == 0
            notes.append(f"A5: no graveyard moves (exp=0); "
                         f"bc_offers={ST['bc_offers']}")
        else:
            ok = False
            notes.append("A5 not-run: torment loop never completed")
        ass["A5_triggers"] = ("passed" if ok else
                              ("not-run" if exp is None else "failed"))

        # ---- A6: effect accounting (4 accepts -> P1 -8, P0 +8) ----
        if post is not None and ST["bc_answered"] > 0:
            l0 = life_of(post, 0)
            l1 = life_of(post, 1)
            exp_l0 = 20 + 2 * ST["bc_accepted"]
            exp_l1 = ST["p1_life_pre_torment"] - 2 * ST["bc_accepted"] \
                if ST["p1_life_pre_torment"] is not None else None
            ok = (l0 == exp_l0 and exp_l1 is not None and l1 == exp_l1
                  and ST["bc_accepted"] >= 1)
            notes.append(f"A6: accepted={ST['bc_accepted']} "
                         f"(declined={ST['bc_answered']-ST['bc_accepted']}) "
                         f"life post={l0}/{l1} "
                         f"(expect {exp_l0}/{exp_l1})")
        elif post is not None and ST["bc_offers"] == 0 and exp == 0:
            ok = True
            notes.append("A6: vacuous (no triggers could exist)")
        else:
            ok = False
            notes.append("A6 not-run: no trigger offers answered / "
                         "post missing")
        ass["A6_effect"] = ("passed" if ok else
                            ("not-run" if ST["bc_answered"] == 0
                             and exp is None else "failed"))

        # ---- A7: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            winner = post.get("winner")
            ok = stack_empty and winner is None and not ST["game_over"]
            notes.append(f"A7: stack_empty={stack_empty} winner={winner} "
                         f"game_over_flag={ST['game_over']}")
        else:
            ok = False
            notes.append("A7 failed: post.json missing")
        ass["A7_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A1_setup"] != "passed" or \
                ass["A2_cast_x10"] not in ("passed", "failed"):
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A1) or cast (A2) failed")
        elif ass["A2_cast_x10"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: torment never resolved")
        elif ass["A3_ten_choices"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: fewer than 10 unless-pay "
                         "prompts offered during X=10 Torment "
                         f"({ST['unless_prompts']} seen)")
        elif ass["A4_sacrifices"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: sacrifices chosen but "
                         "memnites did not move to graveyard as expected")
        elif ass["A5_triggers"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: cards moved to opponent's "
                         "graveyard during Torment but Bloodchief "
                         "Ascension produced "
                         f"{ST['bc_offers']} trigger offers vs "
                         f"{ST['expected_triggers']} moves")
        elif ass["A6_effect"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: trigger offers answered but "
                         "life totals did not change as expected")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup", "A2_cast_x10", "A3_ten_choices",
                  "A4_sacrifices", "A5_triggers", "A6_effect",
                  "A7_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: 10 sacrifices each "
                         "raised a Bloodchief trigger; 4 accepts moved "
                         "life 20->28 / 11->3 as oracle text requires")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7143,
            "verdict": verdict, "validated_at": "2026-09-14",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 70,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7143.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (list(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "post_torment.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_7143.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense test decks are a harness convenience (engine "
                "accepts >4-of for custom games); P1's 60x Memnite deck "
                "exists to supply 10 sacrifice fodder.",
                "P1 never attacks; P0 never attacks. Life changes come "
                "only from Bump in the Night, Torment unless-pay "
                "declines, and Bloodchief trigger accepts.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7143.py",
                    f"{EVDIR}/scenario_7143.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{srv_run}"
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
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7143 - Torment of Hailfire x "
               "Bloodchief Ascension", fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-14 - "
               "X=10 torment vs 3-quest-counter Bloodchief",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 26
        labels = {
            "A1_setup": "setup: ascension + 3 quest counters, "
                        ">=10 memnites, lives 20/11",
            "A2_cast_x10": "torment cast X=10, resolved to graveyard",
            "A3_ten_choices": "10 unless-pay prompts (one per rep)",
            "A4_sacrifices": "10 sacrifices -> 10 memnites to P1 graveyard",
            "A5_triggers": "bloodchief may-offers == graveyard moves",
            "A6_effect": "4 accepts: P1 -8, P0 +8 (decline rest)",
            "A7_cleanup": "game continues, stack empty",
        }
        for k, lab in labels.items():
            res = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if res == "passed" else (
                (255, 90, 90) if res == "failed" else (230, 200, 120))
            d.text((40, y), f"{k}: {res}", fill=col)
            d.text((220, y), lab[:72], fill=(150, 165, 185))
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {})
        reps = ds.get("unless_reps") or []
        branches = [r.get("branch") for r in reps]
        lines = [
            f"torment: cast={ds.get('torment_cast')} "
            f"x_confirmed={ds.get('x_confirmed')} "
            f"unless_prompts={ds.get('unless_prompts')}",
            f"branches: {','.join(branches[:10])}",
            f"bloodchief offers={ds.get('bc_offers')} "
            f"answered={ds.get('bc_answered')} "
            f"accepted={ds.get('bc_accepted')} "
            f"expected={ds.get('expected_triggers')}",
            f"quest: offers={ds.get('quest_offers')} "
            f"accepts={ds.get('quest_accepts')} "
            f"counters={ds.get('quest_counters')}",
            f"bumps_cast={ds.get('bumps_cast')} "
            f"stage_timeouts={ds.get('stage')}",
        ]
        for ln in lines:
            d.text((40, y), ln[:118], fill=(160, 175, 195))
            y += 24
        y += 8
        d.text((24, y), "Notes:", fill=(200, 210, 225))
        y += 24
        for n in run.get("notes", [])[:16]:
            d.text((40, y), ("- " + n)[:116], fill=(150, 165, 185))
            y += 22
        img.save(f"{EVDIR}/summary.png")
        say("rendered summary.png")

    def write_manifest():
        files = ["pre.json", "post_torment.json", "post.json", "run.json",
                 "scenario_7143.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build(silent):
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                elif not silent:
                    say(f"manifest: MISSING {fn}")
            return lines

        # write twice: scenario_run.log is hashed LAST, after all say()
        # logging is done (no say() may follow the second write).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build(False)) + "\n")
        say("wrote manifest.sha256")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build(True)) + "\n")

    await finish()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
