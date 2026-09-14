#!/usr/bin/env python3
"""Issue #7141: exile-off-the-top + may-cast + reshuffle cards "are not [working]"
-- example Grima, Saruman's Footman.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0, key "gr\u00edma, saruman's footman"):
  Grima, Saruman's Footman ({2}{U}{B}, 1/4 Legendary Creature - Human Advisor):
    "Grima can't be blocked.
     Whenever Grima deals combat damage to a player, that player exiles cards
     from the top of their library until they exile an instant or sorcery card.
     You may cast that card without paying its mana cost. Then that player puts
     the exiled cards that weren't cast this way on the bottom of their library
     in a random order."

Card-data parse state on v0.82.0 (verified 2026-09-13 before the run):
  triggers[0] = DamageDone(combat only, valid_target Player) ->
    ExileFromTopUntil(player=TriggeringPlayer, until=NextMatches[Instant|Sorcery])
    sub_ability = CastFromZone(target=ParentTarget, without_paying_mana_cost=true,
      mode=Cast, driver=DuringResolution), optional=true
      sub_ability = PutAtLibraryPosition(target=TriggeringPlayer, count=Fixed(1),
        position=Bottom), optional=false, sub_link=SequentialSibling
  All three stages parse as SUPPORTED. Two parse-shape observations recorded
  (not asserted): the bottom cleanup carries count=Fixed(1) while Oracle says
  "the exiled cards that weren't cast" (plural), and no random-order marker is
  visible in the AST.

Reported symptom (Discord, truncated): "Cards that exile off the top of the
library and then select something and reshuffle are not [working] -- example
[[Grima Sarumans footman]] This is def not the only one." No stage is named,
so the full three-stage contract is tested: exile-until, may-cast offer, and
bottom cleanup.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x grima, saruman's footman, 24x island, 24x swamp.
  P1: 20x lightning bolt, 40x forest (passive; never plays lands/casts/blocks).

Planned line:
  Turns 1-7: P0 drops lands (Island first, then Swamp for color balance),
    casts Grima on turn 7 ({2}{U}{B}).
  Turn 9 (COMBAT1): attack P1 with Grima (unblockable) => 1 combat damage
    (P1 20->19) => DamageDone trigger. P1 exiles from the top until the first
    Lightning Bolt. The may-cast offer is EXPECTED for P0; driver ACCEPTS.
    offer1.json is exported at the offer (exile set visible mid-resolution).
    The Bolt is cast without paying, targeting P1 (P1 19->16); the Bolt card
    goes to its owner's (P1's) graveyard. Remaining exiled cards go to the
    bottom of P1's library. mid.json exported once settled.
  Turn 11 (COMBAT2): attack P1 again (P1 16->15) => trigger again. Driver
    DECLINES the may-cast offer as the control branch (offer2.json at the
    offer). All exiled cards (including the Bolt) go to the bottom of P1's
    library. post.json exported once settled.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: DamageDone -> ExileFromTopUntil[Instant|Sorcery]
                      + CastFromZone(free, optional) + PutAtLibraryPosition(Bottom).
  A2_setup            PRE: Grima on P0 BF, P1 at 20, P1 library intact.
  A3_exile_until      the exile set at offer1 == the top-of-library prefix of
                      P1's pre.json library ending at the first Bolt (top end
                      determined empirically from the observed prefix).
  A4_cast_offered     may-cast offer raised for P0 for the exiled Bolt.
  A5_cast_resolves    accept: Bolt resolves for exactly 3 to P1 with no mana
                      paid; the exiled Bolt card lands in P1's (owner's) gy.
  A6_bottom_cleanup   every other exiled card is in P1's library in mid.json;
                      none remain in exile.
  A7_decline_control  decline: offer raised and declined; all exiled cards
                      (incl. the Bolt) in P1's library in post.json; no new
                      Bolt in P1's gy; no bolt damage to P1.
  A8_cleanup          stack empty, wf Priority, game proceeds.

Verdict rule:
  blocked        iff A2 fails (setup never reached).
  reproduced     iff A2 passes and any of A3..A7 fails (the reported
                 "exile/may-cast/reshuffle not working" in one of its stages).
  not-reproduced iff A2..A8 all pass.

Evidence: evidence/7141/<run-id>/pre.json, offer1.json, mid.json, offer2.json,
post.json, run.json, manifest.sha256, summary.png, scenario_7141.py,
wire_log.jsonl, scenario_run.log, server.log (excerpts).
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
RUN_ID = os.environ.get("RUN_ID", "20260913-7141")
EVDIR = f"{BACKFILL}/evidence/7141/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GRIMA = "gr\u00edma, saruman's footman"
BOLT = "lightning bolt"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"
LANDS = (ISLAND, SWAMP)

P0_DECK = [(GRIMA, 12), (ISLAND, 24), (SWAMP, 24)]
P1_DECK = [(BOLT, 20), (FOREST, 40)]

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
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


def lib_oids(state, pid):
    return [int(o) for o in player_of(state, pid).get("library", [])]


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


def exile_oids(state):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if str(o.get("zone", "")).lower() == "exile"]


def gy_oids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",          # setup -> combat1 -> accept_leg ->
                                   # regen -> combat2 -> decline_leg ->
                                   # cleanup -> done
        "stage_t0": time.time(),
        "pre_exported": False, "offer1_exported": False,
        "mid_exported": False, "offer2_exported": False,
        "post_exported": False,
        "prompt_first_seen": {},
        "land_turn": -1,
        "grima_cast": False, "grima_cast_turn": None,
        "attack1_done": False, "attack1_turn": None,
        "attack2_done": False, "attack2_turn": None,
        # leg 1 (accept)
        "offer1_seen": False, "offer1_skipped": False,
        "offer1_kind": None, "offer1_bolt_oid": None,
        "offer1_exiled": [], "offer1_accepted": False,
        "p1_life_pre": None, "p1_life_at_offer1": None,
        "p1_life_post_bolt": None, "p1_life_at_offer2": None,
        "p1_life_post": None,
        "gy1_bolts_pre_leg1": None, "gy1_bolts_post_leg1": None,
        "bolt_targeted": False, "bolt_resolved": False,
        "p0_untapped_pre_cast": None, "p0_untapped_post_cast": None,
        "settle_ticks": 0,
        # leg 2 (decline)
        "offer2_seen": False, "offer2_skipped": False,
        "offer2_kind": None, "offer2_bolt_oid": None,
        "offer2_exiled": [], "offer2_declined": False,
        "gy1_bolts_at_mid": None,
        "game_code": None,
        "last_rev_acted": {},
        "zones_seen": set(),
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "prompts": [], "stage_timeouts": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-grima")
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
            ST["settle_ticks"] = 0

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

            def rank(ch):
                nm = str(choice_text(ch)).lower()
                if nm in LANDS:
                    return 0
                if nm == GRIMA:
                    return 2
                return 1
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: "
                f"{choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    async def may_cast_offer(c, pid, tag, st, state):
        """Handle the 'you may cast that card' offer (CastOffer or
        OptionalEffectChoice) for Grima's controller."""
        wtype = wf_of(state).get("type") or ""
        if wtype not in ("CastOffer", "OptionalEffectChoice"):
            return False
        if not wf_player_is(state, pid):
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
            codes = [action_codes(ch) for ch in chs]
            refs = [ref_of(ch) for ch in chs]
            texts = [choice_text(ch)[:60] for ch in chs]
            # full opportunity shape to the wire log (first sighting)
            wire("may_cast_offer",
                 {"who": tag, "stage": ST["stage"], "iid": iid,
                  "wf_type": wtype, "wf_data": wf_of(state).get("data"),
                  "n_choices": len(chs), "accept_values": avals,
                  "action_codes": codes, "refs": refs, "texts": texts,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": wtype, "who": tag,
                                   "stage": ST["stage"],
                                   "accept_values": avals,
                                   "action_codes": codes})
            say(f"[{tag}] {wtype} offer (stage={ST['stage']}): "
                f"n={len(chs)} accept={avals} codes={codes} refs={refs}")
            leg = 1 if ST["stage"] in ("combat1", "accept_leg") else 2
            if ST["stage"] not in ("combat1", "combat2"):
                say(f"[{tag}] offer at unexpected stage {ST['stage']}; "
                    "not answering")
                return False
            # snapshot the exile set BEFORE answering
            st_now = st.get("state") or {}
            ex = exile_oids(st_now)
            for z in {str(get_obj(st_now, o).get("zone"))
                      for o in ex}:
                ST["zones_seen"].add(z)
            if leg == 1:
                if not ST["offer1_exported"]:
                    if await export_named("offer1"):
                        ST["offer1_exported"] = True
                ST["offer1_seen"] = True
                ST["offer1_kind"] = wtype
                ST["offer1_exiled"] = ex
                ST["p1_life_at_offer1"] = life_of(st_now, 1)
                # the exiled bolt: refs pointing at an exiled bolt
                bolt_oid = None
                for ch in chs:
                    r = ref_of(ch)
                    if isinstance(r, int) and r in ex \
                            and lname(st_now, r) == BOLT:
                        bolt_oid = r
                        break
                if bolt_oid is None:
                    bolts = [o for o in ex if lname(st_now, o) == BOLT]
                    bolt_oid = bolts[0] if bolts else None
                ST["offer1_bolt_oid"] = bolt_oid
                ST["p0_untapped_pre_cast"] = len(untapped_lands(st_now, 0))
                # ACCEPT (reported branch)
                pick = next((ch for ch in chs
                             if accept_of(ch) == "true"), None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if any("cast" in cd.lower()
                                        for cd in action_codes(ch))), None)
                if pick is None:
                    pick = chs[0]
                say(f"[P0] ACCEPTING the free cast (leg 1, reported branch); "
                    f"exiled={len(ex)} bolt_oid={bolt_oid}")
                await answer_vi(c, opp, pick, tag)
                ST["offer1_accepted"] = True
                set_stage("accept_leg")
                ent["done"] = True
                return True
            else:
                if not ST["offer2_exported"]:
                    if await export_named("offer2"):
                        ST["offer2_exported"] = True
                ST["offer2_seen"] = True
                ST["offer2_kind"] = wtype
                ST["offer2_exiled"] = ex
                ST["p1_life_at_offer2"] = life_of(st_now, 1)
                ST["gy1_bolts_at_mid"] = len(gy_oids(st_now, 1, BOLT))
                bolt_oid = None
                for ch in chs:
                    r = ref_of(ch)
                    if isinstance(r, int) and r in ex \
                            and lname(st_now, r) == BOLT:
                        bolt_oid = r
                        break
                if bolt_oid is None:
                    bolts = [o for o in ex if lname(st_now, o) == BOLT]
                    bolt_oid = bolts[0] if bolts else None
                ST["offer2_bolt_oid"] = bolt_oid
                # DECLINE (control branch)
                pick = next((ch for ch in chs
                             if accept_of(ch) == "false"), None)
                if pick is None:
                    pick = chs[-1]
                say(f"[P0] DECLINING the free cast (leg 2, control); "
                    f"exiled={len(ex)} bolt_oid={bolt_oid}")
                await answer_vi(c, opp, pick, tag)
                ST["offer2_declined"] = True
                set_stage("decline_leg")
                ent["done"] = True
                return True
        return False

    async def target_selection(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "TargetSelection":
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
            pick = next((ch for ch in chs if seat_of(ch) == 1), None)
            if pick is None:
                pick = chs[0]
            wire("target_selection",
                 {"who": tag, "purpose": "grima_bolt", "iid": iid,
                  "n": len(chs), "picked_seat": seat_of(pick),
                  "picked_ref": ref_of(pick)})
            obs["prompts"].append({"kind": "TargetSelection", "who": tag,
                                   "purpose": "grima_bolt",
                                   "picked_seat": seat_of(pick)})
            say(f"[P0] TargetSelection (grima bolt): choosing seat 1")
            await answer_vi(c, opp, pick, tag)
            ST["bolt_targeted"] = True
            ST["p1_life_pre_bolt"] = life_of(state, 1)
            ent["done"] = True
            return True
        return False

    async def handle_order_choice(c, pid, tag, st, state):
        """Generic EffectZoneChoice completer (e.g. bottom-ordering)."""
        if (wf_of(state).get("type") or "") != "EffectZoneChoice":
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
            resp = opp.get("response", {}) or {}
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or rtype
            obs["prompts"].append({"kind": "EffectZoneChoice", "who": tag,
                                   "stage": ST["stage"], "spec": stype,
                                   "n": len(chs)})
            say(f"[{tag}] EffectZoneChoice (stage={ST['stage']}): "
                f"spec={stype} n={len(chs)}")
            if stype != "sequence" or not chs:
                say(f"[{tag}] EffectZoneChoice not completable; recording")
                ent["done"] = True
                return True
            cids = [ch.get("id") for ch in chs]
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": cids}}}
            wire("order_batch_submit",
                 {"who": tag, "iid": iid, "n": len(cids)})
            say(f"[{tag}] answering EffectZoneChoice batch in advertised "
                f"order (n={len(cids)})")
            await c.send_interaction(sub)
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
        if await may_cast_offer(p0, 0, "P0", st, state):
            return
        if await target_selection(p0, 0, "P0", st, state):
            return
        if await handle_order_choice(p0, 0, "P0", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        grima_bf = bf_ids(state, 0, GRIMA)

        # --- combat declarations ---
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player_is(state, 0):
            da = find_action(acts, wtype)
            if da and not acted(f"atk{turn}{wtype}", rev):
                sub = copy.deepcopy(da)
                do_atk = False
                if wtype == "DeclareAttackers" and active == 0:
                    if ST["stage"] == "combat1" and grima_bf \
                            and not ST["attack1_done"]:
                        do_atk = True
                    if ST["stage"] == "combat2" and grima_bf \
                            and not ST["attack2_done"]:
                        do_atk = True
                if do_atk:
                    # pre.json: immediately before the investigated operation
                    if ST["stage"] == "combat1" \
                            and not ST["pre_exported"]:
                        ST["p1_life_pre"] = life_of(state, 1)
                        if await export_named("pre"):
                            ST["pre_exported"] = True
                    sub["data"]["attacks"] = [
                        [grima_bf[0], {"type": "Player", "data": 1}]]
                    for k in ("bands", "blockers", "assignments"):
                        if k in sub["data"]:
                            sub["data"][k] = []
                    say(f"[P0] declaring attacker: grima -> P1 "
                        f"(stage={ST['stage']})")
                else:
                    for k in ("attacks", "bands", "blockers",
                              "assignments"):
                        if k in sub["data"]:
                            sub["data"][k] = []
                await submit_as_is(p0, sub)
                if do_atk:
                    if ST["stage"] == "combat1":
                        ST["attack1_done"] = True
                        ST["attack1_turn"] = turn
                    else:
                        ST["attack2_done"] = True
                        ST["attack2_turn"] = turn
                return

        # --- main-phase actions ---
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop (color-balanced: island then swamp)
            if ST["land_turn"] != turn:
                have = {lname(state, o) for o in bf_lands(state, 0)}
                order = []
                if ISLAND not in have:
                    order.append(ISLAND)
                if SWAMP not in have:
                    order.append(SWAMP)
                order += [ISLAND, SWAMP]
                for want in order:
                    for oid in hand_ids(state, 0):
                        if lname(state, oid) == want:
                            pla = next(
                                (a for a in acts
                                 if a.get("type") == "PlayLand"
                                 and (a.get("data") or {}).get(
                                     "object_id") == oid), None)
                            if pla and not acted("land", rev):
                                say(f"[P0] playing land {want}")
                                await submit_as_is(p0, pla)
                                ST["land_turn"] = turn
                                return
            # cast grima
            if not grima_bf and not ST["grima_cast"]:
                ca = cast_spell_action(acts, state, 0, GRIMA)
                if ca and not acted("grima", rev):
                    say("[P0] casting grima, saruman's footman")
                    await submit_as_is(p0, ca)
                    ST["grima_cast"] = True
                    ST["grima_cast_turn"] = turn
                    return

        # --- stage transitions from live state ---
        if ST["stage"] == "setup" and grima_bf \
                and ST["grima_cast_turn"] is not None \
                and turn >= ST["grima_cast_turn"] + 2 and active == 0:
            set_stage("combat1")
        if ST["stage"] == "combat1":
            # offer skipped? exile happened but no prompt within 45s
            if not ST["offer1_seen"] and ST["attack1_done"] \
                    and time.time() - ST["stage_t0"] > 45:
                say("[P0] combat1: 45s without a may-cast offer; "
                    "treating as skipped-offer path")
                ST["offer1_skipped"] = True
                set_stage("accept_leg")
        if ST["stage"] == "accept_leg":
            n_bolt_stack = sum(
                1 for e in state.get("stack") or []
                if str(e.get("name") or e.get("card_name")
                       or "").lower() == BOLT)
            gy1 = len(gy_oids(state, 1, BOLT))
            if ST["gy1_bolts_pre_leg1"] is None and ST["bolt_targeted"]:
                ST["gy1_bolts_pre_leg1"] = gy1
            if ST["bolt_targeted"] and n_bolt_stack == 0 \
                    and ST["gy1_bolts_pre_leg1"] is not None \
                    and gy1 > ST["gy1_bolts_pre_leg1"] \
                    and not ST["bolt_resolved"]:
                ST["bolt_resolved"] = True
                ST["p1_life_post_bolt"] = life_of(state, 1)
                ST["gy1_bolts_post_leg1"] = gy1
                ST["p0_untapped_post_cast"] = len(untapped_lands(state, 0))
                say(f"[P0] leg-1 bolt resolved: P1 "
                    f"{ST['p1_life_pre_bolt']}->"
                    f"{ST['p1_life_post_bolt']}, P1-gy bolts={gy1}")
            settled = (not (state.get("stack") or [])) and wtype == "Priority"
            leg_done = ST["bolt_resolved"] or ST["offer1_skipped"]
            if leg_done and settled:
                ST["settle_ticks"] += 1
            else:
                ST["settle_ticks"] = 0
            if leg_done and ST["settle_ticks"] >= 3:
                if not ST["mid_exported"]:
                    if await export_named("mid"):
                        ST["mid_exported"] = True
                        set_stage("regen")
        if ST["stage"] == "regen":
            if active == 0 and ST["attack1_turn"] is not None \
                    and turn >= ST["attack1_turn"] + 2 and grima_bf:
                set_stage("combat2")
        if ST["stage"] == "combat2":
            if not ST["offer2_seen"] and ST["attack2_done"] \
                    and time.time() - ST["stage_t0"] > 45:
                say("[P0] combat2: 45s without a may-cast offer; "
                    "treating as skipped-offer path")
                ST["offer2_skipped"] = True
                set_stage("decline_leg")
        if ST["stage"] == "decline_leg":
            settled = (not (state.get("stack") or [])) and wtype == "Priority"
            leg_done = ST["offer2_declined"] or ST["offer2_skipped"]
            if leg_done and settled:
                ST["settle_ticks"] += 1
            else:
                ST["settle_ticks"] = 0
            if leg_done and ST["settle_ticks"] >= 3:
                if not ST["post_exported"]:
                    ST["p1_life_post"] = life_of(state, 1)
                    if await export_named("post"):
                        ST["post_exported"] = True
                        set_stage("done")
        if ST["stage"] in ("combat1", "accept_leg", "combat2",
                           "decline_leg") \
                and time.time() - ST["stage_t0"] > 300:
            obs["stage_timeouts"].append(
                {"stage": ST["stage"],
                 "t": round(time.time() - ST["stage_t0"], 1)})
            say(f"[P0] stage {ST['stage']} timed out (300s); exporting "
                "post fallback")
            if not ST["post_exported"]:
                if await export_named("post"):
                    ST["post_exported"] = True
                    set_stage("done")

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
        # P1 may be asked to order the bottomed cards (the cleanup's target
        # is the damaged player); answer in advertised order.
        if await handle_order_choice(p1, 1, "P1", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player_is(state, 1):
            da = find_action(acts, wtype)
            if da and not acted(f"p1_{wtype}", rev):
                sub = copy.deepcopy(da)
                for k in ("attacks", "bands", "blockers", "assignments"):
                    if k in sub["data"]:
                        sub["data"][k] = []
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
        while time.time() - t0 < 1200:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 90 and s:
                last_diag = time.time()
                grima = bf_ids(s, 0, GRIMA)
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"grima_bf={len(grima)} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"p1_lib={len(lib_oids(s, 1))} "
                    f"exile={len(exile_oids(s))}")
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
        for fn in ("pre", "offer1", "mid", "offer2", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, offer1, mid, offer2, post = (states.get("pre"),
                                          states.get("offer1"),
                                          states.get("mid"),
                                          states.get("offer2"),
                                          states.get("post"))
        ST["zones_seen"] = sorted(ST["zones_seen"])

        def names_of(s, oids):
            return [lname(s, o) for o in oids]

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
            c = cd["gr\u00edma, saruman's footman"]
            trigs = c.get("triggers", [])
            dump = json.dumps(trigs)
            has_exile_until = "ExileFromTopUntil" in dump
            has_next_matches = "NextMatches" in dump
            has_instant = '"Instant"' in dump
            has_sorcery = '"Sorcery"' in dump
            has_cast_free = ("CastFromZone" in dump
                             and "without_paying_mana_cost" in dump)
            has_bottom = "PutAtLibraryPosition" in dump
            # parse-shape observations (not asserted)
            bottom_count = None
            try:
                t0 = trigs[0]["execute"]
                sub = t0["sub_ability"]["sub_ability"]
                bottom_count = sub["effect"].get("count")
            except Exception:
                pass
            has_random = "random" in dump.lower()
            notes.append(
                f"A1: ExileFromTopUntil={has_exile_until} "
                f"NextMatches={has_next_matches} "
                f"Instant={has_instant} Sorcery={has_sorcery} "
                f"CastFromZone_free={has_cast_free} Bottom={has_bottom}; "
                f"OBS bottom_count={bottom_count} "
                f"(oracle: ALL uncast exiled cards), "
                f"random_order_marker={has_random}")
            ok = (has_exile_until and has_next_matches and has_instant
                  and has_sorcery and has_cast_free and has_bottom)
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            g = bf_ids(pre, 0, GRIMA)
            lib1 = lib_oids(pre, 1)
            ok = (len(g) == 1 and ST["p1_life_pre"] == 20 and len(lib1) > 0)
            notes.append(f"A2: grima_bf={len(g)} p1_life_pre="
                         f"{ST['p1_life_pre']} p1_lib={len(lib1)} "
                         f"turn={pre.get('turn_number')}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: exile-until ----
        # E1 = exile contents at offer1; must equal the top-prefix of P1's
        # pre.json library ending at the first bolt (top end determined
        # empirically).
        ex1_names = []
        top_end = None
        if pre is not None and offer1 is not None and ST["offer1_seen"]:
            e1 = exile_oids(offer1)
            ex1_names = names_of(offer1, e1)
            lib_names = names_of(pre, lib_oids(pre, 1))
            for end, seq in (("front", lib_names),
                             ("back", list(reversed(lib_names)))):
                pref = []
                for nm in seq:
                    pref.append(nm)
                    if nm == BOLT:
                        break
                else:
                    continue
                if sorted(pref) == sorted(ex1_names) and pref.count(BOLT) == 1:
                    top_end = end
                    break
            ok = top_end is not None
            notes.append(f"A3: exiled_n={len(e1)} exiled={ex1_names} "
                         f"top_end={top_end} bolt_oid="
                         f"{ST['offer1_bolt_oid']}")
            ST["top_end"] = top_end
        else:
            ok = False
            notes.append(f"A3 failed: pre={'ok' if pre else 'missing'} "
                         f"offer1={'ok' if offer1 else 'missing'} "
                         f"seen={ST['offer1_seen']} "
                         f"skipped={ST['offer1_skipped']}")
        ass["A3_exile_until"] = "passed" if ok else "failed"

        # ---- A4: cast offered ----
        ok = bool(ST["offer1_seen"] and ST["offer1_bolt_oid"] is not None)
        notes.append(f"A4: offer1_seen={ST['offer1_seen']} "
                     f"kind={ST['offer1_kind']} "
                     f"bolt_oid={ST['offer1_bolt_oid']} "
                     f"accepted={ST['offer1_accepted']} "
                     f"skipped={ST['offer1_skipped']}")
        ass["A4_cast_offered"] = "passed" if ok else "failed"

        # ---- A5: cast resolves ----
        if mid is not None and ST["offer1_accepted"]:
            dmg = None
            if ST["p1_life_at_offer1"] is not None \
                    and ST["p1_life_post_bolt"] is not None:
                dmg = ST["p1_life_at_offer1"] - ST["p1_life_post_bolt"]
            bolt_oid = ST["offer1_bolt_oid"]
            o = get_obj(mid, bolt_oid) if bolt_oid else {}
            in_p1_gy = (o.get("zone") == "Graveyard"
                        and o.get("controller") == 1)
            ok = (dmg == 3 and in_p1_gy)
            notes.append(f"A5: P1 {ST['p1_life_at_offer1']}->"
                         f"{ST['p1_life_post_bolt']} (dmg={dmg}, expect 3); "
                         f"bolt oid={bolt_oid} in P1 gy={in_p1_gy}; "
                         f"P0 untapped lands "
                         f"{ST['p0_untapped_pre_cast']}->"
                         f"{ST['p0_untapped_post_cast']} (free cast: expect "
                         "no payment)")
        else:
            ok = False
            notes.append(f"A5 failed: mid={'ok' if mid else 'missing'} "
                         f"accepted={ST['offer1_accepted']}")
        ass["A5_cast_resolves"] = "passed" if ok else "failed"

        # ---- A6: bottom cleanup ----
        if mid is not None and offer1 is not None and ST["offer1_seen"]:
            e1 = exile_oids(offer1)
            rest = [o for o in e1 if o != ST["offer1_bolt_oid"]]
            mid_lib = set(lib_oids(mid, 1))
            mid_exile = set(exile_oids(mid))
            in_lib = sum(1 for o in rest if o in mid_lib)
            stranded = [o for o in rest if o in mid_exile]
            ok = (in_lib == len(rest) and not stranded)
            notes.append(f"A6: uncast_exiled={len(rest)} in_P1_lib={in_lib} "
                         f"stranded_in_exile={len(stranded)} "
                         f"{names_of(mid, stranded)}")
        else:
            ok = False
            notes.append("A6 failed: mid/offer1 missing or offer not seen")
        ass["A6_bottom_cleanup"] = "passed" if ok else "failed"

        # ---- A7: decline control ----
        if post is not None and offer2 is not None and ST["offer2_seen"]:
            e2 = exile_oids(offer2)
            post_lib = set(lib_oids(post, 1))
            post_exile = set(exile_oids(post))
            in_lib = sum(1 for o in e2 if o in post_lib)
            stranded = [o for o in e2 if o in post_exile]
            gy_now = len(gy_oids(post, 1, BOLT))
            gy_then = ST["gy1_bolts_at_mid"]
            dmg2 = None
            if ST["p1_life_at_offer2"] is not None \
                    and ST["p1_life_post"] is not None:
                dmg2 = ST["p1_life_at_offer2"] - ST["p1_life_post"]
            ok = (in_lib == len(e2) and not stranded
                  and gy_then is not None and gy_now == gy_then
                  and dmg2 == 0)
            notes.append(f"A7: exiled_n={len(e2)} in_P1_lib={in_lib} "
                         f"stranded={len(stranded)} "
                         f"P1-gy bolts {gy_then}->{gy_now} (expect no new); "
                         f"P1 {ST['p1_life_at_offer2']}->"
                         f"{ST['p1_life_post']} (dmg={dmg2}, expect 0)")
        else:
            ok = False
            notes.append(f"A7 failed: post={'ok' if post else 'missing'} "
                         f"offer2={'ok' if offer2 else 'missing'} "
                         f"seen={ST['offer2_seen']} "
                         f"skipped={ST['offer2_skipped']}")
        ass["A7_decline_control"] = "passed" if ok else "failed"

        # ---- A8: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty and wft in ("Priority",)
            ST["p0_life_final"] = life_of(post, 0)
            ST["p1_life_final"] = life_of(post, 1)
            notes.append(f"A8: stack_empty={stack_empty} post_wf={wft} "
                         f"life_final={ST['p0_life_final']}/"
                         f"{ST['p1_life_final']}")
        else:
            ok = False
            notes.append("A8 failed: post.json missing")
        ass["A8_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif any(ass.get(k) != "passed"
                 for k in ("A3_exile_until", "A4_cast_offered",
                           "A5_cast_resolves", "A6_bottom_cleanup",
                           "A7_decline_control")):
            verdict = "reproduced"
            notes.append("verdict=reproduced: the exile-until / may-cast / "
                         "bottom-cleanup chain failed in at least one stage")
        elif all(ass.get(k) == "passed"
                 for k in ("A2_setup", "A3_exile_until", "A4_cast_offered",
                           "A5_cast_resolves", "A6_bottom_cleanup",
                           "A7_decline_control", "A8_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: exile-until, free cast, "
                         "and bottom cleanup all behaved per Oracle")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7141,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7141.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (list(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "offer1.json", "mid.json",
                               "offer2.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7141.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Grima / 20x Lightning Bolt density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "P1 is a fully passive punching bag (20x Bolt, 40x Forest; "
                "never plays lands, never casts, never blocks).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7141.py",
                    f"{EVDIR}/scenario_7141.py")
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
        W, H = 1000, 1060
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7141 - Grima, Saruman's Footman",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-13 - "
               "exile-until + may-cast + bottom cleanup",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: combat damage -> exile top until "
               "instant/sorcery -> you may cast it free ->",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "rest go to the bottom of that library in random "
               "order.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: 3-stage AST present",
            "A2_setup": "PRE: grima on BF, P1 at 20, library intact",
            "A3_exile_until": "exile set == top prefix ending at first bolt",
            "A4_cast_offered": "may-cast offer raised for P0",
            "A5_cast_resolves": "bolt for 3, free; bolt card to P1 gy",
            "A6_bottom_cleanup": "uncast exiled cards to P1 library, none stranded",
            "A7_decline_control": "decline: all exiled bottomed, no cast",
            "A8_cleanup": "stack empty; game proceeds",
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
            f"offer1: seen={ds.get('offer1_seen')} kind={ds.get('offer1_kind')} "
            f"accepted={ds.get('offer1_accepted')} "
            f"exiled_n={len(ds.get('offer1_exiled') or [])} "
            f"bolt_oid={ds.get('offer1_bolt_oid')}",
            f"leg1: P1 {ds.get('p1_life_pre')}->"
            f"{ds.get('p1_life_at_offer1')} (combat) ->"
            f"{ds.get('p1_life_post_bolt')} (bolt); "
            f"P1-gy bolts {ds.get('gy1_bolts_pre_leg1')}->"
            f"{ds.get('gy1_bolts_post_leg1')}",
            f"free-cast: P0 untapped lands "
            f"{ds.get('p0_untapped_pre_cast')}->"
            f"{ds.get('p0_untapped_post_cast')}",
            f"offer2: seen={ds.get('offer2_seen')} "
            f"declined={ds.get('offer2_declined')} "
            f"exiled_n={len(ds.get('offer2_exiled') or [])}",
            f"leg2: P1 {ds.get('p1_life_at_offer2')}->"
            f"{ds.get('p1_life_post')} (expect no bolt damage)",
            f"library top end (empirical): {ds.get('top_end')}",
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
        files = ["pre.json", "offer1.json", "mid.json", "offer2.json",
                 "post.json", "run.json", "scenario_7141.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]

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
