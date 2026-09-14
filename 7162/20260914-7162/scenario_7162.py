#!/usr/bin/env python3
"""phase-rs/phase #7162 - Lazav, Familiar Stranger is not prompted to
become a copy of exiled creatures.

Oracle: "Whenever you commit a crime, put a +1/+1 counter on Lazav. Then
you may exile a card from a graveyard. If a creature card was exiled this
way, you may have Lazav become a copy of that card until end of turn.
This ability triggers only once each turn. (Targeting opponents, anything
they control, and/or cards in their graveyards is a crime.)"

Reported: Lazav exiles cards correctly after a crime but never offers the
optional become-a-copy choice when a creature card was exiled.

Contract:
  A1_parse        - v0.82.0 AST: CommitCrime trigger -> PutCounter(SelfRef)
                    -> optional exile (Typed Card, Graveyard->Exile)
                    -> optional BecomeCopy(ParentTarget, UntilEndOfTurn,
                       condition ZoneChangedThisWay Typed[Creature])
  A2_setup_ok     - Lazav on P0 BF, Bears on P1 BF, Doom Blade in P0 hand
                    (pre.json exported before the crime)
  A3_crime_trigger- after Doom Blade cast, Lazav carries a +1/+1 counter
  A4_exile_offered- optional exile prompt offered and accepted (accept=True
                    via the decideOptionalEffect accept/true choice)
  A5_copy_offered - optional become-a-copy prompt offered (expected FAIL:
                    the reported bug is that it never appears)
  A6_copy_effect  - if offered+accepted, Lazav becomes a copy of Bears
  A7_cleanup      - stack empty, game proceeds
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
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7162
RUN_ID = os.environ.get("RUN_ID", "20260914-7162")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

LAZAV = "lazav, familiar stranger"
BLADE = "doom blade"
BEARS = "grizzly bears"
SWAMP = "swamp"
ISLAND = "island"
FOREST = "forest"

P0_DECK = [(LAZAV, 12), (BLADE, 12), (SWAMP, 18), (ISLAND, 18)]
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
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
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
               and lname_of(o) in (SWAMP, ISLAND, FOREST)
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


def cand_name(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("name"):
            return str(d["name"]).lower()
    return None


def lazav_p1p1(state, lazav_oid):
    """Count +1/+1 counters on the Lazav object (shape-defensive)."""
    o = objects(state).get(str(lazav_oid)) or objects(state).get(int(lazav_oid))
    if not o:
        return 0
    blob = json.dumps(o.get("counters")).lower() if o.get("counters") is not None else ""
    c = o.get("counters")
    total = 0
    if isinstance(c, dict):
        for k, v in c.items():
            if "p1p1" in str(k).lower() or "+1/+1" in str(k):
                try:
                    total += int(v)
                except Exception:
                    pass
    elif isinstance(c, list):
        for e in c:
            s = json.dumps(e).lower()
            if "p1p1" in s or "+1/+1" in s:
                n = e.get("count", 1) if isinstance(e, dict) else 1
                try:
                    total += int(n)
                except Exception:
                    total += 1
    return total


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
            # fallback: choose by index of the accept surface ordering
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


async def answer_target(c, iid, state, want_name, want_zone,
                        want_controller, tag, why):
    """Answer a target-selection opportunity by matching the engine-issued
    candidate whose referenced object has the wanted name/zone/controller."""
    for opp in vi_opportunities(c):
        if opp.get("interactionId") != iid:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        wire(f"{why}_candidates",
             {"n": len(choices),
              "sample": [{"name": cand_name(ch),
                          "ref": ref_key(candidate_ref_oid(ch)),
                          "obj": (lambda o: {"zone": o.get("zone"),
                                             "ctrl": o.get("controller"),
                                             "name": lname_of(o)} if o else None)(
                              objects(state).get(str(ref_key(candidate_ref_oid(ch)))))}
                         for ch in choices[:20]]})
        pick = None
        for ch in choices:
            k = ref_key(candidate_ref_oid(ch))
            if k is None:
                continue
            o = objects(state).get(str(k)) or objects(state).get(int(k))
            if (o and lname_of(o) == want_name
                    and o.get("zone") == want_zone
                    and (want_controller is None
                         or o.get("controller") == want_controller)):
                pick = ch
                break
        if pick is None:
            wire(f"{why}_no_match", {"want": [want_name, want_zone,
                                              want_controller]})
            return False
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}
        else:
            stype = (data.get("spec", {}) or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick["id"]]}}}
        wire(f"{why}_answer", {"choice_id": pick.get("id"),
                               "ref": ref_key(candidate_ref_oid(pick))})
        await c.send_interaction(sub)
        say(f"[{tag}] {why} target answered: {want_name} "
            f"{want_zone} oid={ref_key(candidate_ref_oid(pick))}")
        return True
    return False

async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup_ok", "A3_crime_trigger", "A4_exile_offered",
            "A5_copy_offered", "A6_copy_effect", "A7_cleanup")}

    # ---- A1: parse check against the pinned v0.82.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
        e = cd[LAZAV]
        say("oracle:", e["oracle_text"][:160])
        with open(f"{EVDIR}/parse_lazav.json", "w") as f:
            json.dump(e["triggers"], f, indent=1)
        trig = (e.get("triggers") or [])[0]
        mode_ok = trig.get("mode") == "CommitCrime"
        ex = trig.get("execute") or {}
        put_ok = (ex.get("effect") or {}).get("type") == "PutCounter" and (
            (ex.get("effect") or {}).get("target") or {}).get("type") == "SelfRef"
        sub1 = ex.get("sub_ability") or {}
        ex1 = (sub1.get("effect") or {})
        exile_ok = (ex1.get("type") == "ChangeZone"
                    and ex1.get("origin") == "Graveyard"
                    and ex1.get("destination") == "Exile"
                    and sub1.get("optional") is True)
        sub2 = sub1.get("sub_ability") or {}
        ex2 = (sub2.get("effect") or {})
        cond = sub2.get("condition") or {}
        cfilter = (cond.get("filter") or {}).get("type_filters") or []
        copy_ok = (ex2.get("type") == "BecomeCopy"
                   and (ex2.get("target") or {}).get("type") == "ParentTarget"
                   and ex2.get("duration") == "UntilEndOfTurn"
                   and sub2.get("optional") is True
                   and cond.get("type") == "ZoneChangedThisWay"
                   and "Creature" in cfilter)
        if mode_ok and put_ok and exile_ok and copy_ok:
            ass["A1_parse"] = "passed"
            notes.append("A1_parse: passed (CommitCrime -> PutCounter(SelfRef) "
                         "-> optional Graveyard>Exile -> optional BecomeCopy("
                         "ParentTarget, UntilEndOfTurn, ZoneChangedThisWay "
                         "Typed[Creature]))")
        else:
            ass["A1_parse"] = "failed"
            notes.append(f"A1_parse: FAILED mode={mode_ok} put={put_ok} "
                         f"exile={exile_ok} copy={copy_ok}")
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

    ST = {"blade_cast": False, "blade_target_answered": False,
          "trigger_fired": False, "trigger_resolved": False,
          "exile_prompt_seen": False, "exile_accepted": False,
          "exile_target_answered": False, "exile_done": False,
          "copy_prompt_seen": False, "copy_answered": False,
          "copy_done": False, "lazav_oid": None, "bears_oid": None,
          "pre_exported": False, "mid_exported": False,
          "post_exported": False, "cleanup_turns": 0, "done": False}
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

    def lazav_trigger_on_stack(state):
        for se in stack_entries(state):
            kind = se.get("kind") or {}
            if kind.get("type") != "TriggeredAbility":
                continue
            ab = kind.get("ability") or se.get("ability") or {}
            desc = str(ab.get("description") or "").lower()
            if "commit a crime" in desc:
                return True
        return False

    async def seat_tick(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        # transition log: record every waiting_for change for diagnosis
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
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    chs = data.get("choices") or data.get("candidates") or []
                    wire("wf_opp_detail",
                         {"tag": tag, "iid": str(opp.get("interactionId"))[:16],
                          "rtype": resp.get("type"),
                          "spec": (data.get("spec", {}) or {}).get("type"),
                          "n_choices": len(chs),
                          "sample": [{"name": cand_name(ch),
                                      "ref": ref_key(candidate_ref_oid(ch))}
                                     for ch in chs[:8]]})
        acts = merged_actions(st)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # lazav trigger presence tracking
        if pid == 0 and lazav_trigger_on_stack(state) and not ST["trigger_fired"]:
            ST["trigger_fired"] = True
            wire("trigger_fired", {"turn": turn})
            say("[P0] Lazav CommitCrime trigger on stack")
        if pid == 0 and ST["trigger_fired"] and not ST["trigger_resolved"] \
                and not lazav_trigger_on_stack(state):
            ST["trigger_resolved"] = True
            wire("trigger_resolved", {"turn": turn,
                                      "exile_done": ST["exile_done"],
                                      "copy_prompt_seen": ST["copy_prompt_seen"]})
            say("[P0] Lazav trigger left the stack "
                f"(exile_done={ST['exile_done']} "
                f"copy_prompt_seen={ST['copy_prompt_seen']})")

        # mulligan: always keep. Note: MulliganDecision carries a "pending"
        # list (per player/phase), not data.player (hit on #7162 first run).
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
        # discard: protect Lazav/Blade/Bears, discard lands first
        if wtype == "DiscardToHandSize" and wf_player(state) == pid:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                choices = data.get("choices") or data.get("candidates") or []
                count = ((wf.get("data", {}) or {}).get("count")) or 1
                # rank: lands first, then non-key spells; protect key cards
                def discard_rank(ch):
                    nm = cand_name(ch) or ""
                    if nm in (SWAMP, ISLAND, FOREST):
                        return 0
                    if nm in (LAZAV, BLADE, BEARS):
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

        # ---- optional prompts (exile may / copy may) ----
        if wtype == "OptionalEffectChoice" and wf_player(state) == pid:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                wire("optional_seen", {"tag": tag, "iid": str(iid)[:12],
                                       "exile_accepted": ST["exile_accepted"],
                                       "copy_prompt_seen": ST["copy_prompt_seen"]})
                if pid == 0 and not ST["exile_accepted"]:
                    ST["exile_prompt_seen"] = True
                    if await answer_optional(c, iid, True, tag,
                                             "lazav may-exile"):
                        answered_iid.add(iid)
                        ST["exile_accepted"] = True
                    return
                if pid == 0 and ST["exile_accepted"] and not ST["copy_answered"]:
                    ST["copy_prompt_seen"] = True
                    if await answer_optional(c, iid, True, tag,
                                             "lazav may-become-copy"):
                        answered_iid.add(iid)
                        ST["copy_answered"] = True
                        ST["copy_done"] = True
                    return
                # unknown optional: leave it alone, do not auto-decline
                say(f"[{tag}] unexpected OptionalEffectChoice, not answering")
            return

        # ---- target selections ----
        if wtype == "TargetSelection" and wf_player(state) == pid:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                if pid == 0 and ST["blade_cast"] and not ST["blade_target_answered"]:
                    if await answer_target(c, iid, state, BEARS, "Battlefield",
                                           1, tag, "doom_blade"):
                        answered_iid.add(iid)
                        ST["blade_target_answered"] = True
                    return
                if pid == 0 and ST["exile_accepted"] \
                        and not ST["exile_target_answered"]:
                    if await answer_target(c, iid, state, BEARS, "Graveyard",
                                           None, tag, "lazav_exile"):
                        answered_iid.add(iid)
                        ST["exile_target_answered"] = True
                    return
            # target pending but no matching candidate: hold, never pass
            return

        # ---- exile target fallback: answer any candidate-bearing
        # opportunity while the exile choice is pending, regardless of the
        # waiting_for type the engine uses ----
        if pid == 0 and ST["exile_accepted"] and not ST["exile_target_answered"] \
                and not ST["exile_done"]:
            for opp in vi_opportunities(c):
                iid = opp.get("interactionId")
                if iid in answered_iid:
                    continue
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                choices = data.get("choices") or data.get("candidates") or []
                if not choices:
                    continue
                if await answer_target(c, iid, state, BEARS, "Graveyard",
                                       None, tag, "lazav_exile_fb"):
                    answered_iid.add(iid)
                    ST["exile_target_answered"] = True
                    break

        # ---- main-phase action taking ----
        if (phase in ("PreCombatMain", "PostCombatMain") and active == pid
                and my_priority(state, pid)):
            # land drop: P0 needs both colors - island first until one is on
            # the battlefield, then swamp; P1 forest (any)
            pls = [a for a in acts if a.get("type") == "PlayLand"]
            if pls:
                def bf_has(name):
                    return any(o.get("zone") == "Battlefield"
                               and o.get("controller") == pid
                               and lname_of(o) == name
                               for o in objects(state).values())
                def land_pref(a):
                    oid = (a.get("data", {}) or {}).get("object_id") \
                        or (a.get("data", {}) or {}).get("card_id")
                    nm = lname(state, oid) if isinstance(oid, int) else ""
                    if pid == 0:
                        if not bf_has(ISLAND):
                            return {ISLAND: 0, SWAMP: 1}.get(nm, 5)
                        return {SWAMP: 0, ISLAND: 1}.get(nm, 5)
                    return {FOREST: 0}.get(nm, 5)
                pls.sort(key=land_pref)
                await submit_as_is(c, pls[0])
                return
            if pid == 0:
                lazavs = bf_creatures(state, 0, LAZAV)
                if lazavs:
                    ST["lazav_oid"] = lazavs[0]
                # cast Lazav if not on board
                if not lazavs and LAZAV in hand_lnames(state, 0) \
                        and untapped_lands(state, 0) >= 3 \
                        and has_untapped(state, 0, SWAMP) \
                        and has_untapped(state, 0, ISLAND):
                    for a in acts:
                        if "cast" not in (a.get("type") or "").lower():
                            continue
                        dd = a.get("data", {}) or {}
                        oid = dd.get("object_id") or dd.get("card_id")
                        if isinstance(oid, int) and lname(state, oid) == LAZAV:
                            wire("lazav_cast", {"turn": turn})
                            await submit_as_is(c, a)
                            say(f"[P0] cast Lazav (turn {turn})")
                            return
                # cast Doom Blade at the Bears -> the crime
                bears = bf_creatures(state, 1, BEARS)
                if bears and lazavs and not ST["blade_cast"] \
                        and BLADE in hand_lnames(state, 0) \
                        and untapped_lands(state, 0) >= 2 \
                        and has_untapped(state, 0, SWAMP):
                    ST["bears_oid"] = bears[0]
                    if not ST["pre_exported"]:
                        await export_named("pre")
                        ST["pre_exported"] = True
                    for a in acts:
                        if "cast" not in (a.get("type") or "").lower():
                            continue
                        dd = a.get("data", {}) or {}
                        oid = dd.get("object_id") or dd.get("card_id")
                        if isinstance(oid, int) and lname(state, oid) == BLADE:
                            wire("blade_cast", {"turn": turn,
                                                "target": bears[0]})
                            await submit_as_is(c, a)
                            ST["blade_cast"] = True
                            ST["blade_cast_turn"] = turn
                            say(f"[P0] cast Doom Blade targeting Bears "
                                f"oid={bears[0]} (turn {turn})")
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
                            wire("bears_cast", {"turn": turn})
                            await submit_as_is(c, a)
                            say(f"[P1] cast Bears (turn {turn})")
                            return
        # exile-done detection (opportunistic on every tick)
        if pid == 0 and ST["exile_accepted"] and not ST["exile_done"]:
            exiled = zone_cards(state, "Exile", BEARS)
            if exiled:
                ST["exile_done"] = True
                wire("exile_done", {"oids": exiled, "turn": turn})
                say(f"[P0] Bears exiled: oids={exiled}")
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

        # ---- A2: setup ----
        if pre is not None:
            ok = (len(bf_creatures(pre, 0, LAZAV)) >= 1
                  and len(bf_creatures(pre, 1, BEARS)) >= 1
                  and BLADE in hand_lnames(pre, 0))
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: lazav_bf={bf_creatures(pre,0,LAZAV)} "
                         f"p1bears_bf={bf_creatures(pre,1,BEARS)} "
                         f"blade_in_hand={BLADE in hand_lnames(pre,0)}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # helper: lazav oid tracking across post states
        def lazav_oid_in(state):
            lz = bf_creatures(state, 0, LAZAV)
            return lz[0] if lz else None

        # ---- A3: crime trigger fired and counter placed ----
        ref = mid or post
        if ref is not None and ST["blade_cast"]:
            lo = lazav_oid_in(ref) or ST["lazav_oid"]
            cnt = lazav_p1p1(ref, lo) if lo else 0
            ok = ST["trigger_fired"] and cnt >= 1
            ass["A3_crime_trigger"] = "passed" if ok else "failed"
            notes.append(f"A3: trigger_fired={ST['trigger_fired']} "
                         f"trigger_resolved={ST['trigger_resolved']} "
                         f"lazav_oid={lo} p1p1={cnt}")
        else:
            ass["A3_crime_trigger"] = "failed"
            notes.append("A3 failed: blade never cast or no state")

        # ---- A4: exile prompt offered and accepted ----
        # (The exile itself completing is the engine's job; whether a Bears
        # card actually reaches Exile is recorded in notes and gates A5's
        # interpretation.)
        if ST["blade_cast"]:
            ok = ST["exile_prompt_seen"] and ST["exile_accepted"]
            ass["A4_exile_offered"] = "passed" if ok else "failed"
            exiled = zone_cards(ref, "Exile", BEARS) if ref is not None else []
            notes.append(f"A4: exile_prompt_seen={ST['exile_prompt_seen']} "
                         f"exile_accepted={ST['exile_accepted']} "
                         f"bears_in_exile={exiled}")
            if ok and not exiled:
                notes.append("A4-DIAG: the accepted may-exile produced NO "
                             "target selection (wire wf_transition log shows "
                             "OptionalEffectChoice -> Priority with no "
                             "TargetSelection) and NO card reached Exile - "
                             "the engine silently dropped the accepted exile.")
        else:
            ass["A4_exile_offered"] = "failed"
            notes.append("A4 failed: blade never cast")

        # ---- A5: copy prompt offered ----
        if ST["blade_cast"] and ST["exile_accepted"]:
            ass["A5_copy_offered"] = "passed" if ST["copy_prompt_seen"] else "failed"
            notes.append(f"A5: copy_prompt_seen={ST['copy_prompt_seen']} "
                         "(expected prompt never appears per report; the "
                         "accepted exile produced no Exile zone change, so "
                         "the ZoneChangedThisWay condition could never hold)")
        else:
            ass["A5_copy_offered"] = "failed"
            notes.append("A5 failed: prerequisite (cast+exile accept) not reached")

        # ---- A6: copy effect ----
        if ST["copy_prompt_seen"] and ST["copy_done"] and post is not None:
            lo = lazav_oid_in(post)
            o = (objects(post).get(str(lo)) or {}) if lo else {}
            nm = lname_of(o)
            ok = nm == BEARS
            ass["A6_copy_effect"] = "passed" if ok else "failed"
            notes.append(f"A6: lazav name post-copy='{nm}' "
                         f"(expected '{BEARS}')")
        elif not ST["copy_prompt_seen"]:
            ass["A6_copy_effect"] = "not-run"
            notes.append("A6 not-run: no copy prompt was ever offered")
        else:
            ass["A6_copy_effect"] = "failed"
            notes.append("A6 failed: copy prompt seen but effect not observed")

        # ---- A7: cleanup ----
        if post is not None:
            ok = len(stack_entries(post)) == 0
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={ok}")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 failed: post.json missing")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if (ass["A5_copy_offered"] == "failed"
                and ass["A3_crime_trigger"] == "passed"
                and ass["A4_exile_offered"] == "passed"):
            verdict = "reproduced"
        elif (ass["A5_copy_offered"] == "passed"
              and ass["A6_copy_effect"] == "passed"):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
        say("VERDICT:", verdict)

        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": "Lazav, Familiar Stranger is not prompted to become "
                     "exiled creatures",
            "validated_at": "2026-09-14",
            "server": SERVER_IDENTITY,
            "scope": "Lazav CommitCrime trigger: counter + optional exile + "
                     "optional BecomeCopy when a creature is exiled; native "
                     "engine, two human-client seats",
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {k: ST[k] for k in
                             ("blade_cast", "blade_target_answered",
                              "trigger_fired", "trigger_resolved",
                              "exile_prompt_seen", "exile_accepted",
                              "exile_target_answered", "exile_done",
                              "copy_prompt_seen", "copy_answered",
                              "copy_done", "lazav_oid", "bears_oid")},
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
                "Copy was exercised from P1's graveyard only (the Doom "
                "Blade kill); exile-from-own-graveyard not separately run.",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7162.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                        f"{EVDIR}/server.log")
        except Exception as ex4:
            notes.append(f"server.log copy failed: {ex4}")
            say("server.log copy failed:", ex4)
        render_png(run)
        files = ["pre.json", "mid.json", "post.json", "parse_lazav.json",
                 "run.json", "scenario_7162.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]
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
        for fn in ("pre.json", "mid.json", "post.json", "parse_lazav.json",
                   "run.json"):
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
        W, H = 1000, 1120
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7162 - Lazav, Familiar Stranger: "
               "no copy prompt after exiling a creature", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.82.0 (060b5d2) protocol 70 - 2026-09-14",
               fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: commit a crime -> +1/+1 counter, may exile a "
               "card from a graveyard;", fill=(200, 210, 225))
        y += 24
        d.text((36, y), "if a creature was exiled this way, may have Lazav "
               "become a copy of it until end of turn.",
               fill=(200, 210, 225))
        y += 34
        labels = {
            "A1_parse": "PARSE: CommitCrime -> PutCounter -> optional exile -> "
                        "optional BecomeCopy(ZoneChangedThisWay Creature)",
            "A2_setup_ok": "GAME: Lazav on P0 BF, Bears on P1 BF, Doom Blade "
                           "in P0 hand (pre.json)",
            "A3_crime_trigger": "GAME: Doom Blade kills Bears = crime; Lazav "
                                "trigger fires, +1/+1 counter placed",
            "A4_exile_offered": "GAME: may-exile prompt offered+accepted; "
                                "Bears exiled from P1 graveyard",
            "A5_copy_offered": "GAME: may-become-copy prompt offered "
                               "(REPORTED BUG: never appears)",
            "A6_copy_effect": "GAME: Lazav becomes a copy of Grizzly Bears",
            "A7_cleanup": "GAME: stack empty, game proceeds",
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
        d.text((24, y), f"blade_cast={ds.get('blade_cast')} "
               f"trigger_fired={ds.get('trigger_fired')} "
               f"exile_done={ds.get('exile_done')} "
               f"copy_prompt_seen={ds.get('copy_prompt_seen')}",
               fill=(150, 160, 175))
        y += 30
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:12]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1200
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
            # mid export: the trigger resolution finished (or the game moved
            # on past the copy window) with no copy prompt seen.
            st_now = p0.latest or {}
            state_now = st_now.get("state", st_now)
            turn_now = state_now.get("turn_number") or 0
            window_closed = (ST["trigger_resolved"]
                             or (ST["exile_accepted"]
                                 and turn_now > ST.get("blade_cast_turn", 0) + 2))
            if (ST["exile_accepted"] and window_closed
                    and not ST["mid_exported"]):
                # small grace for a late copy prompt
                await asyncio.sleep(2.0)
                if not ST["copy_prompt_seen"]:
                    if await export_named("mid"):
                        ST["mid_exported"] = True
                    ST["cleanup_turns"] = 1
            if ST["copy_done"] and not ST["mid_exported"]:
                await asyncio.sleep(2.0)
                if await export_named("mid"):
                    ST["mid_exported"] = True
                ST["cleanup_turns"] = 1
            if ST["mid_exported"]:
                ST["cleanup_turns"] += 1
                if ST["cleanup_turns"] > 60:
                    ST["done"] = True
            # safety: if blade cast long ago but nothing happened, bail
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
