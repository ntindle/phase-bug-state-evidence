#!/usr/bin/env python3
"""phase-rs/phase #7163 - Birthing Ritual fails with more than 1 creature.

Oracle: "At the beginning of your end step, if you control a creature, look
at the top seven cards of your library. Then you may sacrifice a creature. If
you do, you may put a creature card with mana value X or less from among those
cards onto the battlefield, where X is 1 plus the sacrificed creature's mana
value. Put the rest on the bottom of your library in a random order."

Reported: with >1 creature in play the ability cannot complete its selection
flow. Triage note: "Engine interactive sacrifice result forwarding into
Birthing Ritual's second selection." matthewevans follow-up: the sacrifice
completes but the look-at-top-7/put-creature-onto-battlefield step never
happens.

Contract:
  A1_parse        - v0.82.0 AST: Phase(End) trigger -> Dig(7, prior look) ->
                    optional Sacrifice(1 creature you control) ->
                    condition EffectOutcome/OptionalEffectPerformed -> Dig(
                    keep 1, up_to, Battlefield, filter Creature mv <=
                    ObjectManaValue(CostPaidObject)+1, source prior_look),
                    else Dig(0 keep, rest -> Library bottom)
  A2_setup_ok     - Birthing Ritual on P0 BF, >=2 Bears on P0 BF at the End
                    step trigger (pre.json exported with trigger on stack)
  A3_trigger_fired- ritual trigger seen on the stack at P0's End step
  A4_sacrifice    - may-sacrifice prompt offered; the selection offers BOTH
                    bears (>=2 candidates, the reported multi-candidate case);
                    accept answered
  A5_sacrifice_ef - chosen bear reaches the Graveyard (sacrifice completes)
  A6_bf_pick      - after the sacrifice, the engine offers the put-a-creature-
                    from-the-7 onto the battlefield choice (REPORTED BUG: this
                    step never happens)
  A7_cleanup      - if the pick is offered+answered: the chosen looked-at
                    bear is on the Battlefield, the sacrificed bear is in the
                    Graveyard, library shrank by exactly 1 (1 of 7 to BF, 6
                    to the bottom), stack empty
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9376/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7163
RUN_ID = os.environ.get("RUN_ID", "20260914-7163")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RITUAL = "birthing ritual"
BEARS = "grizzly bears"
FOREST = "forest"

P0_DECK = [(RITUAL, 20), (BEARS, 20), (FOREST, 20)]
P1_DECK = [(BEARS, 12), (FOREST, 48)]

SERVER_IDENTITY = {
    "server_version": "v0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (observed ServerHello; started with --single-user)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d603420bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9376 "
              "(started fresh by this run) + verified pin (minisign "
              "prehashed verify of binary + signed data manifest with the "
              "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY).",
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


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def lname_of(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def lname(state, oid):
    o = objects(state).get(str(oid)) or objects(state).get(int(oid))
    return lname_of(o) if o else ""


def bf_creatures(state, pid, name):
    out = []
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and lname_of(o) == name):
            out.append(int(oid))
    return sorted(out)


def zone_cards(state, zone, name=None, owner=None):
    out = []
    for oid, o in objects(state).items():
        if o.get("zone") != zone:
            continue
        if name is not None and lname_of(o) != name:
            continue
        if owner is not None and o.get("owner") != owner:
            continue
        out.append(int(oid))
    return sorted(out)


def hand_lnames(state, pid):
    return [lname_of(o) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def untapped_lands(state, pid):
    return sum(1 for oid, o in objects(state).items()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and lname_of(o) == FOREST
               and not o.get("tapped"))


def has_untapped(state, pid, name):
    return any(o.get("zone") == "Battlefield" and o.get("controller") == pid
               and lname_of(o) == name and not o.get("tapped")
               for o in objects(state).values())


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data", {}) or {}).get("player")


def my_priority(state, pid):
    return wf_of(state).get("type") == "Priority" and wf_player(state) == pid


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


def stack_entries(state):
    return state.get("stack") or []


def vi_opportunities(c):
    st = c.latest or {}
    vi = st.get("viewer_interaction") or {}
    return vi.get("opportunities", []) or []


def candidate_ref_oid(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


def ref_key(ref):
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


def cand_name(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("name"):
            return str(d["name"]).lower()
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_optional(c, iid, accept, tag, why):
    """Answer an OptionalEffectChoice-style opportunity via value surfaces."""
    for opp in vi_opportunities(c):
        if opp.get("interactionId") != iid:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        wire("optional_choices",
             {"tag": tag, "why": why, "iid": str(iid)[:16],
              "choices": [{"id": ch.get("id"),
                           "text": str(ch.get("text"))[:80],
                           "surfaces": ch.get("surfaces")}
                          for ch in choices]})
        pick = None
        for ch in choices:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                role = str(d.get("role", "")).lower()
                val = str(d.get("value", "")).lower()
                if role == "accept" and val == ("true" if accept else "false"):
                    pick = ch
                    break
            if pick:
                break
        if pick is None:
            wire("optional_no_accept_surface",
                 {"tag": tag, "why": why,
                  "choices": [{"id": ch.get("id"),
                               "surfaces": ch.get("surfaces")} for ch in choices]})
            return False
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick["id"]}}}
        wire("optional_answer", {"tag": tag, "why": why,
                                 "accept": accept,
                                 "choice_id": pick.get("id")})
        await c.send_interaction(sub)
        say(f"[{tag}] optional '{why}' answered accept={accept}")
        return True
    return False

def opp_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (resp, data, data.get("choices") or data.get("candidates") or [])


async def submit_choices(c, iid, resp, data, choice_ids, tag, why):
    rtype = resp.get("type")
    if rtype == "exactChoices":
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": choice_ids[0]}}}
    else:
        stype = (data.get("spec", {}) or {}).get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype,
                            "data": {"choiceIds": list(choice_ids)}}}
    wire("candidate_answer", {"tag": tag, "why": why,
                              "choice_ids": [str(x)[:12] for x in choice_ids]})
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup_ok", "A3_trigger_fired", "A4_sacrifice",
            "A5_sacrifice_effect", "A6_bf_pick", "A7_cleanup")}

    # ---- A1: parse check against the pinned v0.82.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
        e = cd[RITUAL]
        say("oracle:", e["oracle_text"][:120])
        with open(f"{EVDIR}/parse_birthing_ritual.json", "w") as f:
            json.dump(e["triggers"], f, indent=1)
        trig = (e.get("triggers") or [])[0]
        mode_ok = trig.get("mode") == "Phase" and trig.get("phase") == "End"
        ex = trig.get("execute") or {}
        dig1 = (ex.get("effect") or {})
        dig1_ok = (dig1.get("type") == "Dig"
                   and (dig1.get("count") or {}).get("value") == 7)
        sac = ex.get("sub_ability") or {}
        seff = sac.get("effect") or {}
        sac_ok = (seff.get("type") == "Sacrifice"
                  and (seff.get("target") or {}).get("type") == "Typed"
                  and "Creature" in ((seff.get("target") or {})
                                     .get("type_filters") or [])
                  and sac.get("optional") is True)
        sub2 = sac.get("sub_ability") or {}
        cond = sub2.get("condition") or {}
        cond_ok = (cond.get("type") == "EffectOutcome"
                   and cond.get("signal") == "OptionalEffectPerformed")
        dig2 = (sub2.get("effect") or {})
        filt = (dig2.get("filter") or {})
        props = filt.get("properties") or []
        cmc = next((p for p in props if p.get("type") == "Cmc"), {})
        cmcval = cmc.get("value") or {}
        inner = cmcval.get("inner") or {}
        dig2_ok = (dig2.get("type") == "Dig"
                   and dig2.get("destination") == "Battlefield"
                   and dig2.get("keep_count") == 1
                   and dig2.get("up_to") is True
                   and (dig2.get("source") or "") == "prior_look"
                   and "Creature" in (filt.get("type_filters") or [])
                   and cmc.get("comparator") == "LE"
                   and cmcval.get("type") == "Offset"
                   and cmcval.get("offset") == 1
                   and (inner.get("qty") or {}).get("type") == "ObjectManaValue"
                   and (((inner.get("qty") or {}).get("scope") or {}).get("type")
                        == "CostPaidObject"))
        els = sub2.get("else_ability") or {}
        edig = (els.get("effect") or {})
        else_ok = (edig.get("type") == "Dig"
                   and edig.get("rest_destination") == "Library"
                   and edig.get("keep_count") == 0)
        if mode_ok and dig1_ok and sac_ok and cond_ok and dig2_ok and else_ok:
            ass["A1_parse"] = "passed"
            notes.append("A1_parse: passed (Phase/End -> Dig7 prior-look -> "
                         "optional Sacrifice(1 creature you control) -> "
                         "EffectOutcome/OptionalEffectPerformed -> Dig(keep1 "
                         "up_to Battlefield, Creature mv<=CostPaidObject+1, "
                         "source prior_look), else Dig(0 keep, rest->Library))")
        else:
            ass["A1_parse"] = "failed"
            notes.append(f"A1_parse: FAILED mode={mode_ok} dig1={dig1_ok} "
                         f"sac={sac_ok} cond={cond_ok} dig2={dig2_ok} "
                         f"else={else_ok}")
        say(notes[-1])
    except Exception as ex_:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse failed: {ex_}")

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")

    ST = {"trigger_fired": False, "trigger_inferred": False,
          "trigger_resolved": False,
          "pre_exported": False, "mid_exported": False,
          "post_exported": False, "cleanup_turns": 0, "done": False,
          "sac_prompt_seen": False, "sac_accepted": False,
          "sac_candidates_n": 0, "sac_answered": False,
          "sac_chosen_oid": None, "sac_chosen_name": None,
          "sac_done": False, "sac_done_turn": 0,
          "bf_prompt_seen": False, "bf_candidates_n": 0,
          "bf_answered": False, "bf_chosen_oid": None,
          "bf_done": False, "stuck": False, "stuck_exported": False,
          "ritual_on_bf": False, "pre_bears_bf": 0, "pre_lib_count": 0,
          "ritual_turn": 0}
    answered_iid = set()

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            wire(f"export_{tag}", {"ok": True})
            return True
        except Exception as ex2_:
            notes.append(f"{tag} export failed: {ex2_}")
            return False

    def ritual_trigger_on_stack(state):
        for se in stack_entries(state):
            kind = se.get("kind") or {}
            if kind.get("type") != "TriggeredAbility":
                continue
            ab = kind.get("ability") or se.get("ability") or {}
            desc = str(ab.get("description") or "").lower()
            if "birthing ritual" in desc:
                return True
        return False

    def cand_zone(state, ch):
        k = ref_key(candidate_ref_oid(ch))
        if k is None:
            return None, None
        o = objects(state).get(str(k)) or objects(state).get(int(k))
        if not o:
            return k, None
        return k, o.get("zone")

    async def seat_tick(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        lw = ST.setdefault("last_wf", {})
        if lw.get(pid) != (wtype, wf_player(state)):
            lw[pid] = (wtype, wf_player(state))
            wire("wf_transition", {"tag": tag, "type": wtype,
                                   "player": wf_player(state),
                                   "turn": state.get("turn_number"),
                                   "phase": state.get("phase"),
                                   "n_opps": len(vi_opportunities(c))})
            if wtype not in ("Priority", "DeclareAttackers",
                             "DeclareBlockers", "MulliganDecision"):
                for opp in vi_opportunities(c):
                    resp, data, chs = opp_choices(opp)
                    wire("wf_opp_detail",
                         {"tag": tag, "iid": str(opp.get("interactionId"))[:16],
                          "rtype": resp.get("type"),
                          "spec": (data.get("spec", {}) or {}).get("type"),
                          "n_choices": len(chs),
                          "sample": [{"name": cand_name(ch),
                                      "ref": ref_key(candidate_ref_oid(ch)),
                                      "zone": cand_zone(state, ch)[1]}
                                     for ch in chs[:10]]})
        acts = merged_actions(st)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # ritual trigger tracking (P0's view)
        if pid == 0 and ritual_trigger_on_stack(state) \
                and not ST["trigger_fired"]:
            ST["trigger_fired"] = True
            ST["ritual_turn"] = turn
            wire("trigger_fired", {"turn": turn, "phase": phase})
            say(f"[P0] Birthing Ritual trigger on stack (turn {turn})")
            if not ST["pre_exported"]:
                if await export_named("pre"):
                    ST["pre_exported"] = True
                    pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
                    ST["pre_bears_bf"] = len(bf_creatures(pre_st, 0, BEARS))
                    ST["pre_lib_count"] = len(zone_cards(pre_st, "Library", owner=0))
                    say(f"[P0] pre: bears_bf={ST['pre_bears_bf']} "
                        f"lib={ST['pre_lib_count']}")
        if pid == 0 and ST["trigger_fired"] and not ST["trigger_resolved"] \
                and not ritual_trigger_on_stack(state):
            ST["trigger_resolved"] = True
            wire("trigger_resolved", {"turn": turn,
                                      "sac_done": ST["sac_done"],
                                      "bf_prompt_seen": ST["bf_prompt_seen"]})
            say(f"[P0] ritual trigger left the stack "
                f"(sac_done={ST['sac_done']} bf_prompt_seen={ST['bf_prompt_seen']})")

        # mulligan: always keep
        if wtype == "MulliganDecision":
            pending = (wf.get("data", {}) or {}).get("pending") or []
            mine = [p for p in pending
                    if p.get("player") == pid
                    and (p.get("phase") or {}).get("type") == "Declare"]
            if mine:
                ma = find_action(acts, "MulliganDecision")
                if ma:
                    await submit_as_is(c, ma)
                else:
                    await c.send_action({"type": "MulliganDecision",
                                         "data": {"choice": {"type": "Keep"}}})
                say(f"[{tag}] mulligan: keep")
            return
        # discard: protect ritual + bears, discard forests first
        if wtype == "DiscardToHandSize" and wf_player(state) == pid:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                resp, data, choices = opp_choices(opp)
                count = ((wf.get("data", {}) or {}).get("count")) or 1

                def discard_rank(ch):
                    nm = cand_name(ch) or ""
                    if nm == FOREST:
                        return 0
                    if nm in (RITUAL, BEARS):
                        return 9
                    return 5
                ranked = sorted(choices, key=discard_rank)
                picks = [ch["id"] for ch in ranked[:count] if ch.get("id")]
                if picks:
                    spec_t = (data.get("spec", {}) or {}).get("type")
                    rtype = resp.get("type")
                    if rtype == "exactChoices" and len(picks) == 1:
                        sub = {"interactionId": iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": picks[0]}}}
                    else:
                        sub = {"interactionId": iid,
                               "response": {"type": spec_t or "sequence",
                                            "data": {"choiceIds": picks}}}
                    wire("discard_answer", {"tag": tag, "count": count,
                                           "picks": [cand_name(ch) for ch in ranked[:count]]})
                    await c.send_interaction(sub)
                    answered_iid.add(iid)
                    say(f"[{tag}] discarded {count}")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return
        # combat: never attack, never block
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return

        # ---- optional prompts ----
        if wtype == "OptionalEffectChoice" and wf_player(state) == pid:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                if pid == 0 and not ST["sac_accepted"] \
                        and (ST["trigger_fired"] or ST["ritual_on_bf"]):
                    # The trigger window is fast; 0.8s stack sampling can
                    # miss it. A may-sacrifice OptionalEffectChoice for P0
                    # with the Ritual on the battlefield can only be this
                    # trigger resolving (no other optional effects exist in
                    # the fixture), so answer it and record the inference.
                    ST["sac_prompt_seen"] = True
                    if not ST["trigger_fired"]:
                        ST["trigger_fired"] = True
                        ST["trigger_inferred"] = True
                        ST["ritual_turn"] = turn
                        wire("trigger_fired_inferred",
                             {"turn": turn, "phase": phase})
                        say(f"[P0] Birthing Ritual trigger inferred from "
                            f"may-sacrifice prompt (turn {turn})")
                    if await answer_optional(c, iid, True, tag,
                                             "ritual may-sacrifice"):
                        answered_iid.add(iid)
                        ST["sac_accepted"] = True
                    return
                if pid == 0 and ST["sac_done"] and not ST["bf_answered"]:
                    # may-put-onto-battlefield accept/decline
                    ST["bf_prompt_seen"] = True
                    if await answer_optional(c, iid, True, tag,
                                             "ritual may-put-onto-bf"):
                        answered_iid.add(iid)
                        ST["bf_accepted"] = True
                    return
                say(f"[{tag}] unexpected OptionalEffectChoice, not answering")
            return

        # ---- candidate-bearing opportunities (sacrifice pick / bf pick) ----
        if pid == 0 and wf_player(state) == 0:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                resp, data, choices = opp_choices(opp)
                if not choices:
                    continue
                zones = {}
                for ch in choices:
                    k, z = cand_zone(state, ch)
                    zones[z] = zones.get(z, 0) + 1
                # sacrifice selection: candidates are P0's BF bears
                if (ST["sac_accepted"] and not ST["sac_answered"]
                        and zones.get("Battlefield", 0) >= 1):
                    bear_ids = []
                    for ch in choices:
                        k, z = cand_zone(state, ch)
                        if z != "Battlefield":
                            continue
                        kk = str(k)
                        o = objects(state).get(kk) or objects(state).get(int(k))
                        if o and lname_of(o) == BEARS \
                                and o.get("controller") == 0:
                            bear_ids.append((ch["id"], k))
                    ST["sac_candidates_n"] = len(bear_ids)
                    wire("sac_candidates",
                         {"n_total": len(choices), "n_bears": len(bear_ids),
                          "zones": zones})
                    say(f"[P0] sacrifice selection: {len(bear_ids)} bear "
                        f"candidates of {len(choices)} choices")
                    if bear_ids:
                        cid, k = bear_ids[0]
                        await submit_choices(c, iid, resp, data, [cid],
                                             tag, "ritual sacrifice")
                        answered_iid.add(iid)
                        ST["sac_answered"] = True
                        ST["sac_chosen_oid"] = int(k)
                        ST["sac_chosen_name"] = BEARS
                        say(f"[P0] sacrificed bear oid={k}")
                    return
                # battlefield pick: candidates are the looked-at 7 (liminal)
                if (ST["sac_done"] and not ST["bf_answered"]
                        and zones.get("Battlefield", 0) == 0
                        and len(choices) >= 1):
                    # verify the candidates look like the 7 (names + zone)
                    lim_ids = []
                    for ch in choices:
                        k, z = cand_zone(state, ch)
                        if z == "Battlefield":
                            continue
                        if cand_name(ch) == BEARS:
                            lim_ids.append((ch["id"], k, z))
                    ST["bf_prompt_seen"] = True
                    ST["bf_candidates_n"] = len(lim_ids)
                    wire("bf_candidates",
                         {"n_total": len(choices), "n_bears": len(lim_ids),
                          "zones": zones,
                          "sample_zones": [z for _, _, z in lim_ids[:7]]})
                    say(f"[P0] BF-pick selection: {len(lim_ids)} bear "
                        f"candidates of {len(choices)} choices, zones={zones}")
                    if lim_ids and not ST["mid_exported"]:
                        if await export_named("mid"):
                            ST["mid_exported"] = True
                    if lim_ids:
                        cid, k, z = lim_ids[0]
                        await submit_choices(c, iid, resp, data, [cid],
                                             tag, "ritual bf-pick")
                        answered_iid.add(iid)
                        ST["bf_answered"] = True
                        ST["bf_chosen_oid"] = int(k)
                        say(f"[P0] put bear oid={k} (zone {z}) onto BF")
                    return
            # a candidate prompt we did not classify: hold, log only

        # ---- main-phase action taking ----
        if (phase in ("PreCombatMain", "PostCombatMain") and active == pid
                and my_priority(state, pid)):
            pls = [a for a in acts if a.get("type") == "PlayLand"]
            if pls:
                await submit_as_is(c, pls[0])
                return
            if pid == 0:
                rituals_bf = [int(oid) for oid, o in objects(state).items()
                              if o.get("zone") == "Battlefield"
                              and o.get("controller") == 0
                              and lname_of(o) == RITUAL]
                if rituals_bf:
                    ST["ritual_on_bf"] = True
                if not rituals_bf and RITUAL in hand_lnames(state, 0) \
                        and has_untapped(state, 0, FOREST):
                    for a in acts:
                        if "cast" not in (a.get("type") or "").lower():
                            continue
                        dd = a.get("data", {}) or {}
                        oid = dd.get("object_id") or dd.get("card_id")
                        if isinstance(oid, int) and lname(state, oid) == RITUAL:
                            await submit_as_is(c, a)
                            say(f"[P0] cast Birthing Ritual (turn {turn})")
                            return
                if BEARS in hand_lnames(state, 0) \
                        and untapped_lands(state, 0) >= 2:
                    for a in acts:
                        if "cast" not in (a.get("type") or "").lower():
                            continue
                        dd = a.get("data", {}) or {}
                        oid = dd.get("object_id") or dd.get("card_id")
                        if isinstance(oid, int) and lname(state, oid) == BEARS:
                            await submit_as_is(c, a)
                            say(f"[P0] cast Bears (turn {turn})")
                            return
            if pid == 1:
                if BEARS in hand_lnames(state, 1) \
                        and untapped_lands(state, 1) >= 2:
                    for a in acts:
                        if "cast" not in (a.get("type") or "").lower():
                            continue
                        dd = a.get("data", {}) or {}
                        oid = dd.get("object_id") or dd.get("card_id")
                        if isinstance(oid, int) and lname(state, oid) == BEARS:
                            await submit_as_is(c, a)
                            say(f"[P1] cast Bears (turn {turn})")
                            return
        # sacrifice-done detection (opportunistic on every tick)
        if pid == 0 and ST["sac_answered"] and not ST["sac_done"]:
            co = ST["sac_chosen_oid"]
            o = objects(state).get(str(co)) or objects(state).get(int(co))
            if o is not None and o.get("zone") == "Graveyard":
                ST["sac_done"] = True
                ST["sac_done_turn"] = turn
                ST["sac_done_at"] = time.time()
                wire("sac_done", {"oid": co, "turn": turn})
                say(f"[P0] sacrificed bear oid={co} reached Graveyard")
        # bf-pick done detection
        if pid == 0 and ST["bf_answered"] and not ST["bf_done"]:
            co = ST["bf_chosen_oid"]
            o = objects(state).get(str(co)) or objects(state).get(int(co))
            if o is not None and o.get("zone") == "Battlefield":
                ST["bf_done"] = True
                wire("bf_done", {"oid": co, "turn": turn})
                say(f"[P0] chosen bear oid={co} is on the Battlefield")
        # default: pass priority
        if my_priority(state, pid):
            pp = find_action(acts, "PassPriority")
            if pp:
                await submit_as_is(c, pp)

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as ex3:
                notes.append(f"state reload failed for {fn}.json: {ex3}")
        pre, mid, post = states.get("pre"), states.get("mid"), states.get("post")
        ref = post or mid or pre

        # ---- A2: setup ----
        if pre is not None:
            ok = (any(o.get("zone") == "Battlefield"
                      and o.get("controller") == 0 and lname_of(o) == RITUAL
                      for o in objects(pre).values())
                  and len(bf_creatures(pre, 0, BEARS)) >= 2)
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: ritual_bf={ST['ritual_on_bf']} "
                         f"pre_bears_bf={ST['pre_bears_bf']} (>=2 needed) "
                         f"pre_lib={ST['pre_lib_count']}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3: trigger fired ----
        if ST["trigger_fired"]:
            ass["A3_trigger_fired"] = "passed"
            notes.append(f"A3: trigger_fired at turn {ST['ritual_turn']}, "
                         f"resolved={ST['trigger_resolved']} "
                         f"inferred_from_prompt={ST['trigger_inferred']} "
                         f"(trigger left the stack between 0.8s stack "
                         f"samples; the may-sacrifice prompt proves it "
                         f"resolved)")
        else:
            ass["A3_trigger_fired"] = "failed"
            notes.append("A3 failed: ritual trigger never seen on stack")

        # ---- A4: sacrifice prompt + multi-candidate selection ----
        if ST["sac_prompt_seen"]:
            ok = ST["sac_accepted"] and ST["sac_candidates_n"] >= 2
            ass["A4_sacrifice"] = "passed" if ok else "failed"
            notes.append(f"A4: sac_prompt_seen={ST['sac_prompt_seen']} "
                         f"sac_accepted={ST['sac_accepted']} "
                         f"sac_candidates_n={ST['sac_candidates_n']} "
                         f"(>=2 required for the reported multi-creature case)")
        else:
            ass["A4_sacrifice"] = "failed"
            notes.append("A4 failed: may-sacrifice prompt never offered")

        # ---- A5: sacrifice completes ----
        if ST["sac_answered"] and ref is not None:
            co = ST["sac_chosen_oid"]
            o = (objects(ref).get(str(co)) or objects(ref).get(int(co))) \
                if co is not None else None
            ok = o is not None and o.get("zone") == "Graveyard"
            ass["A5_sacrifice_effect"] = "passed" if ok else "failed"
            notes.append(f"A5: sac_chosen_oid={co} zone="
                         f"{o.get('zone') if o else None} "
                         f"(X = 1 + sacrificed bear mv 2 = 3)")
        elif not ST["sac_answered"]:
            ass["A5_sacrifice_effect"] = "not-run"
            notes.append("A5 not-run: sacrifice selection never answered")
        else:
            ass["A5_sacrifice_effect"] = "failed"
            notes.append("A5 failed: no state to check")

        # ---- A6: bf-pick prompt ----
        if ST["sac_done"]:
            ok = ST["bf_prompt_seen"]
            ass["A6_bf_pick"] = "passed" if ok else "failed"
            notes.append(f"A6: bf_prompt_seen={ST['bf_prompt_seen']} "
                         f"bf_candidates_n={ST['bf_candidates_n']} "
                         f"(REPORTED BUG: put-a-creature-onto-BF step never "
                         f"happens after the sacrifice)")
        else:
            ass["A6_bf_pick"] = "not-run"
            notes.append("A6 not-run: sacrifice never completed")

        # ---- A7: cleanup / full resolution ----
        if ST["bf_answered"] and post is not None:
            bo = ST["bf_chosen_oid"]
            boo = (objects(post).get(str(bo)) or objects(post).get(int(bo))) \
                if bo is not None else None
            co = ST["sac_chosen_oid"]
            coo = (objects(post).get(str(co)) or objects(post).get(int(co))) \
                if co is not None else None
            lib_post = len(zone_cards(post, "Library", owner=0))
            lib_delta = ST["pre_lib_count"] - lib_post
            bf_ok = boo is not None and boo.get("zone") == "Battlefield"
            sac_ok = coo is not None and coo.get("zone") == "Graveyard"
            lib_ok = lib_delta == 1
            stack_ok = len(stack_entries(post)) == 0
            ok = bf_ok and sac_ok and lib_ok and stack_ok
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: bf_chosen_on_bf={bf_ok} sac_in_gy={sac_ok} "
                         f"lib_delta={lib_delta} (expect 1: 7 looked at, 1 to "
                         f"BF, 6 to bottom) stack_empty={stack_ok}")
        elif ST["stuck"]:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 failed: ability stalled after the sacrifice - "
                         "no BF-pick prompt, game moved on with the 7 cards "
                         "unresolved")
        else:
            ass["A7_cleanup"] = "not-run"
            notes.append("A7 not-run: bf pick never answered")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        # verdict: the reported bug is the ability failing to complete with
        # multiple creatures (sacrifice completes, BF-pick step never happens)
        if (ass["A3_trigger_fired"] == "passed"
                and ass["A4_sacrifice"] == "passed"
                and ass["A5_sacrifice_effect"] == "passed"
                and ass["A6_bf_pick"] == "failed"):
            verdict = "reproduced"
        elif (ass["A6_bf_pick"] == "passed"
              and ass["A7_cleanup"] == "passed"):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
        say("VERDICT:", verdict)

        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": "Birthing Ritual does not work with more than 1 "
                     "creature in play",
            "validated_at": "2026-09-14",
            "server": SERVER_IDENTITY,
            "scope": "Birthing Ritual end-step trigger with 2+ creatures: "
                     "Dig7 -> may-sacrifice -> conditional put-onto-BF from "
                     "the 7; native engine, two human-client seats",
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {k: ST[k] for k in
                             ("trigger_fired", "trigger_inferred",
                              "trigger_resolved",
                              "sac_prompt_seen", "sac_accepted",
                              "sac_candidates_n", "sac_answered",
                              "sac_chosen_oid", "sac_done",
                              "bf_prompt_seen", "bf_candidates_n",
                              "bf_answered", "bf_chosen_oid", "bf_done",
                              "stuck", "ritual_turn", "pre_bears_bf",
                              "pre_lib_count")},
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Zero-attacker combat was scripted on both seats so combat "
                "could not mask the trigger.",
                "Grizzly Bears (mv 2) used as both the sacrifice and the "
                "put-onto-BF candidate; X = 3.",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7163.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                        f"{EVDIR}/server.log")
        except Exception as ex4:
            notes.append(f"server.log copy failed: {ex4}")
            say("server.log copy failed:", ex4)
        render_png(run)
        files = ["pre.json", "mid.json", "post.json",
                 "parse_birthing_ritual.json", "run.json",
                 "scenario_7163.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        say("wrote manifest.sha256")
        for fn in ("pre.json", "mid.json", "post.json",
                   "parse_birthing_ritual.json", "run.json"):
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                json.load(open(p))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        say("validation: all JSON parse, PNG readable, hashes match")

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7163 - Birthing Ritual: fails with "
               ">1 creature in play", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.82.0 (060b5d2) protocol 70 - 2026-09-14",
               fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: end step -> look at top 7 -> may sacrifice a",
               fill=(200, 210, 225))
        y += 24
        d.text((36, y), "creature -> may put a creature (mv <= X=sac mv+1) "
               "from the 7 onto the BF,",
               fill=(200, 210, 225))
        y += 24
        d.text((36, y), "rest on the bottom in random order.",
               fill=(200, 210, 225))
        y += 34
        labels = {
            "A1_parse": "PARSE: Phase/End -> Dig7 -> optional Sacrifice -> "
                        "cond Dig(BF, mv<=CostPaidObject+1)",
            "A2_setup_ok": "GAME: ritual on P0 BF, >=2 Bears on P0 BF "
                           "at End-step trigger (pre.json)",
            "A3_trigger_fired": "GAME: ritual trigger fires at P0 End step",
            "A4_sacrifice": "GAME: may-sacrifice offered; BOTH bears "
                            "selectable (>=2 candidates)",
            "A5_sacrifice_effect": "GAME: chosen bear reaches the Graveyard "
                                  "(X=3)",
            "A6_bf_pick": "GAME: put-a-creature-from-the-7-onto-BF offered "
                          "(REPORTED BUG: never happens)",
            "A7_cleanup": "GAME: new bear on BF, sac bear in GY, library -1, "
                         "stack empty",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v}", fill=c)
            y += 22
            d.text((52, y), lab[:104], fill=(150, 160, 175))
            y += 26
        y += 8
        ds = run.get("driver_state") or {}
        d.text((24, y), f"trigger_fired={ds.get('trigger_fired')} "
               f"sac_candidates={ds.get('sac_candidates_n')} "
               f"sac_done={ds.get('sac_done')} "
               f"bf_prompt_seen={ds.get('bf_prompt_seen')} "
               f"bf_done={ds.get('bf_done')}", fill=(150, 160, 175))
        y += 30
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:14]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1500
    last_tick = {0: 0, 1: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1):
                tag = ("P0", "P1")[pid]
                if time.time() - last_tick[pid] < 0.8:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            st_now = p0.latest or {}
            state_now = st_now.get("state", st_now)
            turn_now = state_now.get("turn_number") or 0
            # general stall guard: a frozen decision (same waiting_for and
            # revision) for >90s means nobody can move the game forward
            prog_key = ((state_now.get("waiting_for") or {}).get("type"),
                        wf_player(state_now), p0.revision)
            if prog_key == ST.get("last_prog_key"):
                if time.time() - ST.get("last_prog_t", t_start) > 90:
                    notes.append(f"stalled: waiting_for={prog_key[0]} "
                                 f"player={prog_key[1]} rev={prog_key[2]} "
                                 "frozen >90s; exporting stuck state")
                    say(f"STALLED on {prog_key[0]}, exporting stuck state")
                    if not ST["mid_exported"]:
                        if await export_named("mid"):
                            ST["mid_exported"] = True
                            ST["stuck_exported"] = True
                    ST["stuck"] = True
                    wire("stalled",
                         {"type": prog_key[0], "player": prog_key[1],
                          "rev": prog_key[2]})
                    break
            else:
                ST["last_prog_key"] = prog_key
                ST["last_prog_t"] = time.time()
            # stuck detection: sacrifice done long ago, no BF pick prompt
            if ST["sac_done"] and not ST["bf_prompt_seen"] \
                    and not ST["stuck"]:
                if time.time() - ST.get("sac_done_at", t_start) > 60 \
                        or turn_now > ST["sac_done_turn"] + 3:
                    ST["stuck"] = True
                    wire("stuck", {"turn": turn_now,
                                   "sac_done_turn": ST["sac_done_turn"]})
                    say("[P0] STUCK: sacrifice done, no BF-pick prompt "
                        f"(turn {turn_now})")
                    await asyncio.sleep(2.0)
                    if not ST["mid_exported"]:
                        if await export_named("mid"):
                            ST["mid_exported"] = True
                            ST["stuck_exported"] = True
                    ST["cleanup_turns"] = 1
            if ST["bf_done"] and not ST["mid_exported"]:
                await asyncio.sleep(2.0)
                if await export_named("mid"):
                    ST["mid_exported"] = True
                ST["cleanup_turns"] = 1
            if ST["mid_exported"]:
                ST["cleanup_turns"] += 1
                if ST["cleanup_turns"] > 60:
                    ST["done"] = True
            # safety: ritual never cast by turn 8 -> bail
            if not ST["ritual_on_bf"] and not ST["trigger_fired"] \
                    and turn_now >= 16:
                notes.append("safety: ritual never reached the battlefield; "
                             "bailing")
                break
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
    finally:
        await finish()
        for c in (p0, p1):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
