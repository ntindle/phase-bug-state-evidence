#!/usr/bin/env python3
"""Issue #6201: "[Card Bug] Thor, God of Thunder: Exiled card can't be played".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-19, source: GitHub): Thor, God of Thunder's enters trigger
exiled Too Evil to Stay Dead (sorcery) from the graveyard; on the reporter's
next turn the exiled sorcery was not offered to cast. Attached game state
(game-state-turn-14-...json) is at the Draw step, where a sorcery cannot
normally be cast, so it does not establish the defect by itself.

Oracle text (verified from pinned v0.78.0 card-data.json):
  Thor, God of Thunder {3}{R}{R} 5/5 Legendary Creature - God Warrior Hero,
  Flying.
  "When Thor enters, exile target Equipment, instant, or sorcery card from
   your graveyard. Until the end of your next turn, you may play that card."
  "Whenever you cast a noncreature spell, Thor deals damage equal to that
   spell's mana value to any target."
Parsed (v0.78.0 data): ChangesZone trigger (Graveyard->Exile, Equipment /
instant / sorcery filter) with a GrantCastingPermission sub-ability
(PlayFromExile, UntilEndOfNextTurnOf Controller) on a TrackedSet.

Triage acceptance criteria (mike-theDude, 2026-07-20):
  - The card is offered during the next turn whenever its ordinary timing,
    targeting, and cost requirements are met.
  - It is not offered at illegal timing.
  - The permission expires after that turn.

Scenario (native engine, two human-client seats):
  P0: 8x Thor, God of Thunder + 8x Too Evil to Stay Dead + 8x Faithless
      Looting + 6x Grizzly Bears + 15x Mountain + 15x Swamp.
  P1: 12x Grizzly Bears + 48x Forest (passive; plays a land, never attacks).

  GAME A (reported path + full outcome):
    - P0 casts Faithless Looting, discards Too Evil to Stay Dead + Grizzly
      Bears (the Bears doubles as the MV<=4 creature-card target for the
      sorcery).
    - P0 casts Thor ({3}{R}{R}); ETB trigger exiles Too Evil to Stay Dead.
    - A2 (illegal timing control): on P0's next turn, at Draw phase priority
      and during DeclareAttackers, the exiled sorcery must NOT be offered.
    - A3: on P0's next turn PreCombatMain, CastSpell(Too Evil to Stay Dead)
      must be advertised and actionable.
    - A4: cast it for {2}{B}; target the Bears; Thor's noncreature trigger
      deals 3 to P1; assert P1 20->17, Bears returns to battlefield, sorcery
      in P0 graveyard, stack empty, game proceeds.
  GAME B (expiry):
    - Same setup; the exiled sorcery is deliberately NOT cast on the next
      turn. On the turn after next (permission expired), PreCombatMain must
      not offer it, and the card must still be in exile.

Assertions:
  A1_setup_ok        post_etb.json: Thor on P0 BF, Too Evil to Stay Dead in
                     P0 exile.
  A2_illegal_timing  next-turn Draw-phase and DeclareAttackers priority: no
                     CastSpell offered for the exiled sorcery.
  A3_offered_next_turn  next-turn PreCombatMain: CastSpell advertised.
  A4_cast_completes  post_cast.json: P1 20->17, Bears on P0 BF, sorcery in
                     P0 gy, stack empty, game proceeding, zero rejections.
  A5_permission_expires  turn-after-next PreCombatMain: not offered; card
                     still in exile.
  A6_cleanup         final states: stack empty, game at Priority, no stall.

Verdict rule: reproduced iff A3 fails (not offered at legal timing on the
next turn) or A4 fails (the cast path breaks). not-reproduced iff A1, A3,
A4, A5 pass. blocked iff A1 fails (setup never reaches the exile).

RUN B note (2026-09-10): run 20260910-6201 stalled before the exile: its
Looting-discard fallback ranked Thor first-to-discard, so Thor was
discarded to the graveyard and Evil never reached it; the ETB premise never
materialized. This run fixes the discard policy (never discard Thor;
discard Evil, then Bears, then spare Lootings/lands) and retries Looting
while Evil or the Bears target is still missing from the graveyard.

Evidence: evidence/6201/<run-id>/pre_exile.json, post_etb.json,
pre_cast.json, post_cast.json, pre_B.json, post_B.json, run.json,
manifest.sha256, summary.png, scenario_6201b.py, wire_log.jsonl,
scenario_run.log
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
RUN_ID = "20260910-6201b"
EVDIR = f"{BACKFILL}/evidence/6201/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

THOR = "thor, god of thunder"
EVIL = "too evil to stay dead"
LOOT = "faithless looting"
BEARS = "grizzly bears"
MOUNTAIN = "mountain"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [(THOR, 8), (EVIL, 8), (LOOT, 8), (BEARS, 6), (MOUNTAIN, 15),
           (SWAMP, 15)]
P1_DECK = [(BEARS, 12), (FOREST, 48)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-10",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260910-6201b",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


def untapped_lands(state, pid, name=None):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and (name is None or obj_name(o) == name)]


def bf_objects(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            if name is None or obj_name(o) == name:
                out.append(int(oid))
    return out


def exile_oids(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Exile" and o.get("controller") == pid:
            if name is None or obj_name(o) == name:
                out.append(int(oid))
    return out


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("text")):
                t = d.get("name") or d.get("text")
                break
    return str(t)


def choice_status(ch):
    return (ch.get("status") or {}).get("type")


def surf_summary(ch):
    """Compact (type, code, role, value, name) per surface for logging."""
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict):
            out.append((s.get("type"), d.get("code"), d.get("role"),
                        d.get("value"),
                        str(d.get("name") or d.get("text") or "")[:40]))
        else:
            out.append((s.get("type"), str(d)[:40]))
    return out


def ref_oid(ch):
    """Object reference from a candidate's surfaces, or None."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("reference") is not None:
            ref = d["reference"]
            if isinstance(ref, dict):
                rid = ref.get("object_id") or ref.get("id")
            else:
                rid = ref
            try:
                return int(rid)
            except (TypeError, ValueError):
                return None
    return None


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("seat") is not None:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None


def stack_entries(state):
    return state.get("stack", []) or []


def thor_on_bf(state):
    return bool(bf_objects(state, 0, THOR))


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_illegal_timing", "A3_offered_next_turn",
            "A4_cast_completes", "A5_permission_expires", "A6_cleanup")}
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()

    obs = {
        "game": None,          # "A" or "B"
        "game_done": {"A": False, "B": False},
        "answered_ids": [],
        "rejections": [],
        "decisions": [],
        # per-game flags, reset by new_game()
        "g": {},
        "kept": {},
        "stuck_watch_fired": False,
    }

    def new_game(g):
        obs["game"] = g
        obs["g"] = {
            "etb_seen": False,
            "etb_answered": False,
            "exiled_oids": [],
            "thor_turn": None,   # turn Thor was cast (P0's turn)
            "t_next": None,      # P0's next turn after thor_turn
            "t_next2": None,     # P0's turn after t_next (expiry check)
            "looting_pending_discard": False,
            "cast_submitted": False,
            "evil_target_chosen": False,
            "thor_target_chosen": False,
            "cast_done": False,
            "a2_draw_checked": False,
            "a2_draw_offered": False,
            "a2_combat_checked": False,
            "a2_combat_offered": False,
            "a3_offered": False,
            "a3_checked": False,
            "a5_checked": False,
            "a5_offered": False,
            "b_hold_logged": False,
        }

    async def start_game(g):
        nonlocal p0, p1
        new_game(g)
        # Fresh sockets per game: a new game restarts revisions at ~1, and
        # stale high revisions from the previous game would poison the
        # tick loop's latest-state selection.
        for old in (p0, p1):
            try:
                await old.close()
            except Exception:
                pass
        p0 = PhaseClient("P0")
        p1 = PhaseClient("P1")
        await p0.connect()
        await p1.connect()
        await p0.create(deck(*P0_DECK))
        await p1.join(p0.game_code, deck(*P1_DECK))
        say(f"game {g}: code {p0.game_code}; P0 seat={p0.player_id} "
            f"P1 seat={p1.player_id}")

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "game": obs["game"],
                                          "type": t, "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "game": obs["game"],
                                   "type": t, "data": data})

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def log_vi(c, st, tag):
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return
        for opp in opps:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            choices = data.get("choices") or data.get("candidates") or []
            spec = data.get("spec")
            info = {
                "tag": tag, "who": c.name, "game": obs["game"],
                "canSubmit": vi.get("canSubmit"),
                "interactionId": opp.get("interactionId"),
                "otype": opp.get("type"),
                "opptag": opp.get("tag"),
                "rtype": resp.get("type"),
                "spec": (spec.get("type") if isinstance(spec, dict)
                         else spec),
                "prompt": str(opp.get("prompt") or opp.get("title") or "")[:160],
                "n_choices": len(choices),
                "wf_type": (st.get("state", {}).get("waiting_for") or {}).get(
                    "type"),
                "choices": [
                    {"id": ch.get("id"), "text": choice_text(ch)[:80],
                     "status": choice_status(ch),
                     "surfaces": surf_summary(ch)}
                    for ch in choices[:12]],
            }
            wire("vi_opportunity", info)

    async def submit_choice(c, opp, ch, why, response=None):
        iid = opp.get("interactionId")
        if response is None:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            spec = data.get("spec") or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            if isinstance(spec, str):
                stype = spec
            cid = ch.get("id")
            if rtype == "exactChoices":
                response = {"type": "choose", "data": {"choiceId": cid}}
            else:
                rdata = {"choiceIds": [cid]}
                if stype == "manaGroups":
                    max_batch = ((spec.get("data") or {}).get("maxBatch")
                                 if isinstance(spec.get("data"), dict)
                                 else None) or 1
                    rdata["count"] = min(1, max_batch)
                response = {"type": stype or "sequence", "data": rdata}
        sub = {"interactionId": iid, "response": response}
        obs["answered_ids"].append(iid)
        obs["decisions"].append({"game": obs["game"], "why": why,
                                 "interactionId": iid,
                                 "choice": choice_text(ch)[:80]})
        say(f"[{c.name}] {why}: choice {ch.get('id')} "
            f"{choice_text(ch)[:60]!r}")
        wire("decision_submission", {"who": c.name, "game": obs["game"],
                                     "why": why, "interactionId": iid,
                                     "submission": sub})
        await c.send_interaction(sub)

    def castspell_actions(acts):
        return [a for a in acts if a["type"] == "CastSpell"]

    def exiled_evil_offered(acts, state):
        """True if any available action names one of the exiled Evil oids.

        The exile-cast may surface under an action type other than
        CastSpell, so scan every action carrying an object_id.
        """
        g = obs["g"]
        for a in acts:
            oid = (a.get("data", {}) or {}).get("object_id")
            try:
                oid = int(oid)
            except (TypeError, ValueError):
                continue
            if oid in g["exiled_oids"]:
                return True
        return False

    def decide(opp, state):
        """Dispatch schema-sequence prompts by driver state order:
        ETB exile target -> Evil cast target -> Thor damage target.
        Returns (choice, policy) or (None, policy)."""
        g = obs["g"]
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices
        blob = json.dumps(opp, default=str).lower()

        if rtype == "exactChoices":
            # Optional additional costs (e.g. Teamwork): decline them.
            # Detected via the opportunity blob or the authoritative
            # waiting_for type (OptionalCostChoice).
            wf_type = (state.get("waiting_for") or {}).get("type")
            if "teamwork" in blob or wf_type == "OptionalCostChoice":
                for ch in avail:
                    role, val = None, None
                    for s in ch.get("surfaces", []) or []:
                        d = (s.get("data") or {})
                        if (s.get("type") == "value"
                                and isinstance(d, dict)):
                            role, val = d.get("role"), d.get("value")
                    t = choice_text(ch).lower()
                    sval = str(val).lower()
                    if ((role and "decline" in str(role).lower())
                            or sval in ("false", "no", "decline")
                            or any(k in t for k in
                                   ("decline", "don't", "do not", "no",
                                    "skip", "cancel", "without",
                                    "not pay"))):
                        return ch, "teamwork_decline"
                wire("teamwork_no_decline_found",
                     {"interactionId": opp.get("interactionId"),
                      "wf_type": wf_type,
                      "choices": [(c.get("id"), choice_text(c)[:60],
                                   surf_summary(c)) for c in avail]})
                return None, "teamwork_unanswered"
            codes = set()
            for ch in avail:
                for s in ch.get("surfaces", []) or []:
                    d = (s.get("data") or {})
                    if s.get("type") == "action" and isinstance(d, dict):
                        codes.add(d.get("code"))
            if "chooseLegend" in codes:
                def leg_ref(ch):
                    r = ref_oid(ch)
                    return r if r is not None else 10 ** 9
                avail.sort(key=leg_ref)
                return avail[0], "choose_legend_keep_first"
            return None, "exact_unrecognized"

        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(spec, str):
            stype = spec
        # Faithless Looting discard: discard exactly two cards with an
        # explicit desire order — Evil first (we need one in the
        # graveyard), then Bears (the reanimation target), then spare
        # Lootings, then Swamps, then Mountains. NEVER discard Thor
        # (the prior run's rank-based fallback discarded it). Extra
        # copies beyond what the graveyard needs rank as keepers.
        if g["looting_pending_discard"]:
            gy0 = set(gy_names(state, 0))

            def drank(ch):
                t = choice_text(ch).lower()
                if THOR in t:
                    return 10  # never discard Thor
                if EVIL in t:
                    return 0 if EVIL not in gy0 else 6
                if BEARS in t:
                    return 1 if BEARS not in gy0 else 7
                if LOOT in t:
                    return 2  # dead card after the first Looting resolves
                if SWAMP in t:
                    return 3
                if MOUNTAIN in t:
                    return 4
                return 5
            picks = sorted(avail, key=drank)[:2]
            if len(picks) < 2:
                wire("looting_fewer_than_two",
                     {"interactionId": opp.get("interactionId"),
                      "n": len(picks)})
                return None, "looting_fewer_than_two"
            obs["_discard_ids"] = [c.get("id") for c in picks]
            say(f"looting discard picks: "
                f"{[choice_text(c)[:30] for c in picks]}")
            return picks[0], "looting_discard"
        if stype != "sequence":
            return None, f"schema_not_sequence_{stype}"

        # schema sequence: target selection. The engine may surface Thor's
        # cast-trigger target ("any target": players / battlefield
        # objects) BEFORE the spell's own target (a graveyard card), so
        # dispatch by candidate shape, not just driver order.
        if not g["etb_answered"]:
            # Thor ETB: exile target Equipment/instant/sorcery from gy.
            for ch in avail:
                rid = ref_oid(ch)
                if rid is not None and lname(state, rid) == EVIL:
                    return ch, "etb_exile_evil"
            wire("etb_no_evil_candidate",
                 {"candidates": [(c.get("id"), ref_oid(c),
                                  lname(state, ref_oid(c) or -1))
                                 for c in avail]})
            return None, "etb_no_evil_candidate"
        if g["cast_submitted"] and not g["evil_target_chosen"]:
            # Too Evil to Stay Dead: target creature card in gy, MV<=4.
            # Its candidates reference graveyard objects; Thor's damage
            # trigger never does.
            for ch in avail:
                rid = ref_oid(ch)
                if (rid is not None and lname(state, rid) == BEARS
                        and get_obj(state, rid).get("zone") == "Graveyard"):
                    return ch, "evil_target_bears"
        if g["cast_submitted"] and not g["thor_target_chosen"]:
            # Thor damage trigger: any target -> P1.
            for ch in avail:
                if seat_of(ch) == 1:
                    return ch, "thor_damage_p1"
        if g["cast_submitted"] and not g["evil_target_chosen"]:
            wire("evil_no_bears_candidate",
                 {"candidates": [(c.get("id"), ref_oid(c),
                                  lname(state, ref_oid(c) or -1))
                                 for c in avail]})
            return None, "evil_no_bears_candidate"
        if g["cast_submitted"] and not g["thor_target_chosen"]:
            wire("thor_no_p1_candidate",
                 {"candidates": [(c.get("id"), seat_of(ch),
                                  choice_text(c)[:40]) for c in avail]})
            return None, "thor_no_p1_candidate"
        return None, "sequence_unexpected_state"

    async def handle_vi(c, st, state):
        vi = get_vi(st)
        if not vi:
            return False
        # The engine does not label opportunities with a usable tag field;
        # detect the cleanup discard from the authoritative waiting_for
        # type instead.
        wtype = (state.get("waiting_for") or {}).get("type")
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                continue
            # cleanup discard is always safe to answer.
            if (wtype == "DiscardToHandSize"
                    and not obs["g"]["looting_pending_discard"]):
                ch, policy = cleanup_discard(opp, state)
                if ch is not None:
                    ids = obs.pop("_discard_ids", [ch.get("id")])
                    await submit_choice(
                        c, opp, ch, policy,
                        response={"type": "select",
                                  "data": {"choiceIds": ids}})
                    acted = True
                continue
            ch, policy = decide(opp, state)
            if ch is None:
                wire("vi_unanswered", {"who": c.name, "game": obs["game"],
                                      "interactionId": iid,
                                      "policy": policy})
                say(f"[{c.name}] no decision for {iid} (policy={policy})")
                continue
            if policy == "looting_discard":
                ids = obs.pop("_discard_ids", [ch.get("id")])
                await submit_choice(
                    c, opp, ch, policy,
                    response={"type": "select",
                              "data": {"choiceIds": ids}})
                obs["g"]["looting_pending_discard"] = False
            else:
                await submit_choice(c, opp, ch, policy)
            gg = obs["g"]
            if policy == "etb_exile_evil":
                gg["etb_answered"] = True
            if policy == "evil_target_bears":
                gg["evil_target_chosen"] = True
            if policy == "thor_damage_p1":
                gg["thor_target_chosen"] = True
            acted = True
        return acted

    def cleanup_discard(opp, state):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices
        gy = gy_names(state, 0)
        bears_safe = BEARS in gy  # Looting already stocked one

        def drank(ch):
            t = choice_text(ch).lower()
            if LOOT in t:
                return 0  # dead card after the first Looting resolved
            if BEARS in t:
                return 1 if bears_safe else 4
            if THOR in t:
                return 2
            if MOUNTAIN in t or SWAMP in t:
                return 3
            return 4  # EVIL and anything else: keep
        picks = sorted(avail, key=drank)
        n = max(1, len(picks) - 7)
        sel = picks[:n]
        obs["_discard_ids"] = [c.get("id") for c in sel]
        return sel[0], "cleanup_discard"

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    def log_castspells(tag, acts, state):
        bits = []
        for a in castspell_actions(acts):
            oid = a.get("data", {}).get("object_id")
            try:
                nm = lname(state, int(oid))
                zn = get_obj(state, int(oid)).get("zone")
            except (TypeError, ValueError):
                nm, zn = "?", "?"
            bits.append(f"{nm}@{zn}")
        if bits:
            wire("castspell_scan", {"tag": tag, "game": obs["game"],
                                    "offered": bits})

    async def p0_tick(st, acts, state):
        g = obs["g"]
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number", 0)
        phase = state.get("phase")
        active = state.get("active_player")

        # mulligan: hunt 2+ lands (with a Mountain for Looting/Thor),
        # (Thor or Looting), and a Bears (the reanimation target must
        # reach the graveyard via Looting)
        ma = find_action(acts, "MulliganDecision")
        gk = f"P0_{obs['game']}"
        if ma and not obs["kept"].get(gk):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n in (MOUNTAIN, SWAMP))
            mountains = sum(1 for n in hn if n == MOUNTAIN)
            key = THOR in hn or LOOT in hn
            has_bears = BEARS in hn
            mulls = obs["kept"].get(gk + "_m", 0)
            if (lands >= 2 and mountains >= 1 and key
                    and (has_bears or mulls >= 3)) or mulls >= 4:
                obs["kept"][gk] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"game {obs['game']} P0 keeps (lands={lands}, "
                    f"mountains={mountains}, key={key}, bears={has_bears}, "
                    f"mulls={mulls})")
            else:
                obs["kept"][gk + "_m"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"game {obs['game']} P0 mulligans #{mulls + 1} "
                    f"(lands={lands}, mountains={mountains}, key={key}, "
                    f"bears={has_bears})")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not obs["kept"].get(gk + "_b"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]
                # protect: last copy of each key card, last Mountain, and
                # up to 3 lands (Mountains preferred); bottom spare
                # Lootings, then spare Swamps, then spare Mountains.
                mountains = [o for o in hand_ids
                             if lname(state, o) == MOUNTAIN]
                swamps = [o for o in hand_ids
                          if lname(state, o) == SWAMP]
                protected = set((mountains + swamps)[:3])
                for nm in (LOOT, THOR, BEARS, EVIL):
                    cands = [o for o in hand_ids if lname(state, o) == nm]
                    if cands:
                        protected.add(cands[0])

                def bkey(oid):
                    if oid in protected:
                        return 10
                    nm = lname(state, oid)
                    if nm == LOOT:
                        return 0
                    if nm == SWAMP:
                        return 1
                    if nm == MOUNTAIN:
                        return 2
                    return 3
                picks = sorted(hand_ids, key=bkey)[:count]
                obs["kept"][gk + "_b"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x)
                                                           for x in picks]}})
                say(f"game {obs['game']} P0 bottoms {count}")
            return True

        # payments first (land taps for mana)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return True

        if await handle_vi(p0, st, state):
            return True

        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                # A2 illegal-timing scan before declaring (game A only)
                if (obs["game"] == "A" and active == 0 and phase
                        == "DeclareAttackers" and g["t_next"] is not None
                        and turn == g["t_next"]
                        and not g["a2_combat_checked"]):
                    off = exiled_evil_offered(acts, state)
                    g["a2_combat_checked"] = True
                    g["a2_combat_offered"] = off
                    log_castspells("A2_combat", acts, state)
                    say(f"A2 combat check: offered={off}")
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True

        # ETB trigger detection
        if thor_on_bf(state) and not g["etb_seen"]:
            trig = [e for e in stack_entries(state)
                    if "trigger" in json.dumps(e, default=str).lower()]
            if trig:
                g["etb_seen"] = True
                await export("pre_exile" if obs["game"] == "A" else "pre_B_etb")
                wire("etb_seen", {"game": obs["game"], "n": len(trig)})
                say(f"game {obs['game']}: Thor ETB trigger on stack")

        # exile resolution detection
        if (g["etb_answered"] and not g["exiled_oids"]
                and not stack_entries(state)):
            ex = exile_oids(state, 0, EVIL)
            if ex:
                g["exiled_oids"] = ex
                tag = "post_etb" if obs["game"] == "A" else "post_B_etb"
                await export(tag)
                say(f"game {obs['game']}: {EVIL} exiled, oids={ex}")

        if wtype != "Priority" or state.get("priority_player") != 0:
            return False

        own_main = active == 0 and phase in ("PreCombatMain", "PostCombatMain")
        own_draw = active == 0 and phase == "Draw"

        # track P0's own turns after the Thor cast (seats alternate turns,
        # so P0's "next turn" is thor_turn + 2, not + 1)
        if (g["thor_turn"] is not None and active == 0
                and turn > g["thor_turn"]):
            if g["t_next"] is None:
                g["t_next"] = turn
                say(f"game {obs['game']}: P0's next turn after Thor = {turn}")
            elif turn > g["t_next"] and g["t_next2"] is None:
                g["t_next2"] = turn
                say(f"game {obs['game']}: P0's following turn = {turn}")

        # A2 illegal-timing scan at Draw-phase priority (game A), mirroring
        # the reporter's attached Draw-phase state
        if (obs["game"] == "A" and own_draw and g["t_next"] is not None
                and turn == g["t_next"]
                and not g["a2_draw_checked"]):
            off = exiled_evil_offered(acts, state)
            g["a2_draw_checked"] = True
            g["a2_draw_offered"] = off
            log_castspells("A2_draw", acts, state)
            say(f"A2 draw check: offered={off}")

        if own_main:
            # play a land
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return True
            n_mount = len(untapped_lands(state, 0, MOUNTAIN))
            n_any = len(untapped_lands(state, 0))

            if not thor_on_bf(state):
                # setup: cast Looting to stock the graveyard (Evil + Bears).
                # A second Looting is allowed if Evil is stocked but the
                # Bears target never reached the graveyard.
                gy0 = gy_names(state, 0)
                need_bears = (EVIL in gy0 and BEARS not in gy0
                              and BEARS in hand_names(state, 0))
                if ((EVIL not in gy0 or need_bears)
                        and not g["looting_pending_discard"]):
                    lid = spell_in_hand_oid(state, 0, LOOT)
                    if (lid is not None and n_mount >= 1
                            and len(player_of(state, 0).get("hand", [])) >= 3):
                        for a in acts:
                            d = a.get("data", {})
                            if (a["type"] == "CastSpell"
                                    and lname(state, d.get("object_id")) == LOOT):
                                say("P0 casts Faithless Looting")
                                wire("cast_looting", a)
                                g["looting_pending_discard"] = True
                                await submit_as_is(p0, a)
                                return True
                # setup: cast Thor when the graveyard is stocked
                if (EVIL in gy_names(state, 0)
                        and BEARS in gy_names(state, 0)):
                    tid = spell_in_hand_oid(state, 0, THOR)
                    if tid is not None and n_any >= 5 and n_mount >= 2:
                        for a in acts:
                            d = a.get("data", {})
                            if (a["type"] == "CastSpell"
                                    and lname(state, d.get("object_id")) == THOR):
                                say(f"P0 casts Thor (turn {turn})")
                                wire("cast_thor", a)
                                g["thor_turn"] = turn
                                await submit_as_is(p0, a)
                                return True

            # game A: cast the exiled sorcery on P0's NEXT turn only
            if (obs["game"] == "A" and g["exiled_oids"]
                    and g["t_next"] is not None
                    and turn == g["t_next"] and not g["cast_submitted"]):
                off = exiled_evil_offered(acts, state)
                if not g["a3_checked"]:
                    g["a3_checked"] = True
                    g["a3_offered"] = off
                    log_castspells("A3_main", acts, state)
                    say(f"A3 offer check at PreCombatMain: offered={off}")
                if off:
                    # need {2}{B}: 1 untapped Swamp + 2 other untapped lands
                    n_swamp = len(untapped_lands(state, 0, SWAMP))
                    if n_swamp >= 1 and n_any >= 3:
                        for a in acts:
                            oid = (a.get("data", {}) or {}).get("object_id")
                            try:
                                oid = int(oid)
                            except (TypeError, ValueError):
                                continue
                            if oid in g["exiled_oids"]:
                                await export("pre_cast")
                                say(f"P0 casts {EVIL} from exile "
                                    f"(turn {turn})")
                                wire("cast_evil_from_exile", a)
                                g["cast_submitted"] = True
                                await submit_as_is(p0, a)
                                return True
                # if offered but mana short, keep passing until mana is up
            # game A: never cast the exiled card on the cast turn itself
            # (hold for the next-turn window); game B: never cast it at all.
            # game B: capture the still-armed permission at turn+1, then the
            # expiry check at turn+2.
            if (obs["game"] == "B" and g["exiled_oids"]
                    and g["t_next"] is not None
                    and turn == g["t_next"]
                    and phase == "PostCombatMain"
                    and not os.path.exists(f"{EVDIR}/pre_B.json")):
                await export("pre_B")
                say(f"game B: pre_B exported at turn {turn} (permission "
                    f"still armed)")
            if (obs["game"] == "B" and g["exiled_oids"]
                    and g["t_next2"] is not None
                    and turn == g["t_next2"]
                    and not g["a5_checked"]):
                off = exiled_evil_offered(acts, state)
                g["a5_checked"] = True
                g["a5_offered"] = off
                log_castspells("A5_expiry", acts, state)
                say(f"A5 expiry check at turn {turn} PreCombatMain: "
                    f"offered={off}")
                await export("post_B")
                obs["game_done"]["B"] = True
                return True

        # game A completion: sorcery resolved, stack empty
        if (obs["game"] == "A" and g["cast_submitted"]
                and not g["cast_done"]):
            s_gy = gy_names(state, 0)
            if (EVIL in s_gy and not stack_entries(state)
                    and wtype == "Priority"):
                g["cast_done"] = True
                await export("post_cast")
                say("game A: sorcery resolved; exporting post_cast")
                obs["game_done"]["A"] = True
                return True

        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        gk = f"P1_{obs['game']}"
        ma = find_action(acts, "MulliganDecision")
        if ma and not obs["kept"].get(gk):
            obs["kept"][gk] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say(f"game {obs['game']} P1 keeps")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not obs["kept"].get(gk + "_b"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 1:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 1).get("hand", [])]

                def bkey(oid):
                    return 0 if lname(state, oid) == FOREST else 1
                picks = sorted(hand_ids, key=bkey)[:count]
                obs["kept"][gk + "_b"] = True
                await submit_as_is(p1, {"type": "SelectCards",
                                        "data": {"cards": [int(x)
                                                           for x in picks]}})
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return True
        if await handle_vi(p1, st, state):
            return True
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if wtype != "Priority" or state.get("priority_player") != 1:
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        def env(tag):
            p = f"{EVDIR}/{tag}.json"
            return json.loads(open(p).read())["state"] if os.path.exists(p) else None

        pre_exile = env("pre_exile")
        post_etb = env("post_etb")
        pre_cast = env("pre_cast")
        post_cast = env("post_cast")
        pre_B = env("pre_B")
        post_B = env("post_B")

        # A1
        if post_etb is not None:
            thor_bf = thor_on_bf(post_etb)
            evil_ex = bool(exile_oids(post_etb, 0, EVIL))
            ok = thor_bf and evil_ex
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: thor_on_bf={thor_bf}, evil_in_exile={evil_ex}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: post_etb.json missing (exile never resolved)")

        # A2/A3/A5 read from the per-game flag snapshots stored at game end.
        snapA = obs.get("snapA", {})
        if snapA:
            parts = []
            if snapA.get("a2_draw_checked"):
                parts.append(("draw", not snapA.get("a2_draw_offered")))
            if snapA.get("a2_combat_checked"):
                parts.append(("combat", not snapA.get("a2_combat_offered")))
            if not parts:
                ass["A2_illegal_timing"] = "not-run"
                notes.append("A2: neither Draw-phase nor DeclareAttackers "
                             "priority materialized on the next turn")
            elif all(p[1] for p in parts):
                ass["A2_illegal_timing"] = "passed"
                notes.append(f"A2: not offered at illegal timing "
                             f"({', '.join(p[0] for p in parts)} checked)")
            else:
                ass["A2_illegal_timing"] = "failed"
                notes.append(f"A2: OFFERED at illegal timing: "
                             f"{[p[0] for p in parts if not p[1]]}")
        else:
            ass["A2_illegal_timing"] = "not-run"
            notes.append("A2: game A snapshot unavailable")

        # A3
        if snapA.get("a3_checked"):
            ok = bool(snapA.get("a3_offered"))
            ass["A3_offered_next_turn"] = "passed" if ok else "failed"
            notes.append(f"A3: offered at next-turn PreCombatMain="
                         f"{snapA.get('a3_offered')}")
        else:
            ass["A3_offered_next_turn"] = "failed"
            notes.append("A3: next-turn PreCombatMain offer check never ran")

        # A4
        if post_cast is not None:
            s = post_cast
            p1life = life_of(s, 1)
            bears_bf = bool(bf_objects(s, 0, BEARS))
            evil_gy = EVIL in gy_names(s, 0)
            stack_empty = not stack_entries(s)
            wf = (s.get("waiting_for") or {}).get("type")
            rejA = [r for r in obs["rejections"] if r.get("game") == "A"]
            ok = (p1life == 17 and bears_bf and evil_gy and stack_empty
                  and wf == "Priority" and len(rejA) == 0)
            ass["A4_cast_completes"] = "passed" if ok else "failed"
            notes.append(f"A4: P1 life={p1life} (want 17), bears_on_P0_bf="
                         f"{bears_bf}, evil_in_P0_gy={evil_gy}, "
                         f"stack_empty={stack_empty}, wf={wf}, "
                         f"game-A rejections={len(rejA)}")
        else:
            ass["A4_cast_completes"] = "failed"
            notes.append("A4: post_cast.json missing (sorcery never resolved)")

        # A5
        snapB = obs.get("snapB", {})
        if snapB.get("a5_checked") and post_B is not None:
            s = post_B
            evil_ex = bool(exile_oids(s, 0, EVIL))
            ok = evil_ex and not snapB.get("a5_offered")
            ass["A5_permission_expires"] = "passed" if ok else "failed"
            notes.append(f"A5: evil still in exile={evil_ex}, offered at "
                         f"turn+2 PreCombatMain={snapB.get('a5_offered')}")
        else:
            ass["A5_permission_expires"] = "failed"
            notes.append("A5: expiry check never ran or post_B.json missing")

        # A6
        finals = [("post_cast", post_cast), ("post_B", post_B)]
        ok_all = True
        for tag, s in finals:
            if s is None:
                ok_all = False
                continue
            wf = (s.get("waiting_for") or {}).get("type")
            if stack_entries(s) or wf != "Priority":
                ok_all = False
        ass["A6_cleanup"] = "passed" if ok_all else "failed"
        notes.append(f"A6: final states clean={ok_all}, "
                     f"stuck_watch={obs['stuck_watch_fired']}")

        notes.append(f"decisions={len(obs['decisions'])} "
                     f"rejections={len(obs['rejections'])}")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached the Thor exile")
        elif (ass["A3_offered_next_turn"] == "failed"
                or ass["A4_cast_completes"] == "failed"):
            verdict = "reproduced"
            notes.append("the exiled-sorcery cast path did not behave per "
                         "the triage acceptance criteria on this build")
        elif all(ass[k] == "passed" for k in
                 ("A1_setup_ok", "A3_offered_next_turn", "A4_cast_completes",
                  "A5_permission_expires")):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("one or more required assertions failed")
        return verdict

    async def finish():
        dur = time.time() - t_start
        verdict = evaluate()
        run = {
            "issue": 6201,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6201b.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {
                "snapA": obs.get("snapA", {}),
                "snapB": obs.get("snapB", {}),
                "rejections": obs["rejections"],
                "stuck_watch_fired": obs["stuck_watch_fired"],
            },
            "decisions": obs["decisions"],
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x key-card density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The reporter's attached turn-14 state (build 0.30.0, Draw "
                "phase) cannot be imported into the pinned v0.78.0 prebuilt "
                "server (no ImportAuthoritativeState on the release binary); "
                "the scenario replays the reported sequence fresh instead.",
                "Not tested on the original 2026-07-19 build 0.30.0; verdict "
                "is scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x Thor, God of Thunder + 8x Too Evil to Stay "
                          "Dead + 8x Faithless Looting + 6x Grizzly Bears + "
                          "20x Mountain + 10x Swamp; P1: 12x Bears + 48x Forest",
            "contract_line": "Thor ETB exiles Too Evil to Stay Dead; it must "
                             "not be offered at illegal timing, must be "
                             "offered and castable on the next turn "
                             "(P1 20->17 via Thor trigger, Bears reanimated), "
                             "and the permission must expire the turn after",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    with open(f"{EVDIR}/scenario_6201b.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6201b.py").read())

    await start_game("A")

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    game_start_turn_cap = 30
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        # resolve clients per iteration: start_game() rebinds p0/p1 to
        # fresh sockets between games.
        for name, tick in (("P0", p0_tick), ("P1", p1_tick)):
            c = p0 if name == "P0" else p1
            await drain_rejections(c)
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            log_vi(c, st, "tick")
        # game transitions
        if obs["game_done"]["A"] and obs["game"] == "A":
            obs["snapA"] = dict(obs["g"])
            say("game A complete; starting game B (expiry path)")
            await start_game("B")
            last = {}
            last_tick_at = {}
        if obs["game_done"]["B"] and obs["game"] == "B":
            obs["snapB"] = dict(obs["g"])
            say("game B complete; finishing")
            await finish()
            return
        if p0.latest:
            s = p0.latest["state"]
            if s.get("turn_number", 0) >= game_start_turn_cap:
                notes.append(f"turn {game_start_turn_cap} cap hit in game "
                             f"{obs['game']}; finishing to evaluation")
                obs[f"snap{obs['game']}"] = dict(obs["g"])
                say("turn cap; finishing")
                await finish()
                return
        mid_trigger = bool(obs["g"].get("etb_seen") and not obs["g"].get(
            "exiled_oids")) or bool(obs["g"].get("cast_submitted") and not
            obs["g"].get("cast_done"))
        if mid_trigger and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not mid_trigger:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            obs["stuck_watch_fired"] = True
            notes.append(f"STUCK WATCH FIRED (180s): waiting_for={wf} "
                         f"priority_player={s.get('priority_player')}")
            say(f"STUCK WATCH FIRED: waiting_for={wf}")
            try:
                await export("mid_stall")
            except Exception:
                pass
            obs[f"snap{obs['game']}"] = dict(obs["g"])
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG game={obs['game']} turn={s.get('turn_number')} "
                f"active={s.get('active_player')} phase={s.get('phase')} "
                f"wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} hand={hand_names(s,0)[:6]} "
                f"thor_bf={thor_on_bf(s)} "
                f"evil_gy={EVIL in gy_names(s,0)} "
                f"evil_ex={bool(exile_oids(s,0,EVIL))} "
                f"thor_turn={obs['g'].get('thor_turn')}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    obs[f"snap{obs['game']}"] = dict(obs["g"])
    await finish()


asyncio.run(main())
