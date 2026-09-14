#!/usr/bin/env python3
"""Issue #7092: Stuck decision: EffectZoneChoice (Terminus miracle cast).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0, key 'terminus'):
  Terminus ({4}{W}{W} Sorcery):
    "Put all creatures on the bottom of their owners' libraries.
     Miracle {W} (You may cast this card for its miracle cost when you draw
     it if it's the first card you drew this turn.)"

Card-data parse state on v0.82.0 (verified 2026-09-13 before the run):
  abilities[0] = Spell ChangeZoneAll -> Library, target Typed Creature,
    library_position Bottom. keywords = [Miracle {W}].
  The clause parses as SUPPORTED (matches the triage classifier's
  supported_aspect_defect verdict): the defect, if present, is a runtime
  consumption defect on the mass zone move.

Reported symptom (v0.48.0): casting Terminus for its miracle cost leaves the
game stuck at an EffectZoneChoice decision with no documented way to
complete it (priority:p0-softlock).

Engine design on v0.82.0 (read from phase-src-v0.82.0, no code changes):
  - Drawing Terminus as the first card of the turn queues a miracle offer;
    WaitingFor::MiracleReveal -> accept pushes the miracle trigger, which
    resolves into WaitingFor::CastOffer{Miracle}; CastSpellAsMiracle casts
    for {W} (auto-tap).
  - Resolving the ChangeZoneAll->Library/Bottom effect raises one
    WaitingFor::EffectZoneChoice per owner (APNAP, CR 401.4: each player
    chooses the order their own creatures go to the bottom of their
    library), surfaced to the viewer as a HumanResponseModel::Select
    opportunity (schema spec type "select", exact-count constraint).
    Completion is GameAction::SelectCards{cards} in the chosen order.
  - So the historical softlock is expected to be FIXED only if the driver
    can actually complete every owner batch through the viewer interaction.
    If the prompt appears but offers no completable submission, or
    submissions are rejected and the game stalls, the bug REPRODUCES.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 28x terminus, 12x grizzly bears, 10x plains, 10x forest.
  P1: 12x grizzly bears, 48x forest.
  Both sides build boards of Bears. Primary path: P0 draws Terminus as the
  first card of a turn, accepts the miracle reveal / CastOffer only when the
  board is ready (2+ creatures per owner) and {W} is payable, and the
  driver completes every mass-library-order EffectZoneChoice batch.
  Fallback path (if no miracle cast by P0 turn 12): normal-cast Terminus
  for {4}{W}{W} on a main phase with >=6 untapped lands (>=2 Plains).
  Declined miracle offers do NOT block the fallback.

Assertions (each passed / failed / not-run):
  A1_parse             card-data: ChangeZoneAll->Library/Bottom, Typed
                       Creature, Miracle {W} all present and supported.
  A2_setup             PRE: creatures owned by BOTH players on the
                       battlefield at cast time; priority/cast preconditions.
  A3_miracle_offer     a MiracleReveal was raised for a first-draw Terminus
                       (primary path). not-run if RNG never dealt one by the
                       fallback trigger (noted as a limitation).
  A4_miracle_cast      accepted reveal -> miracle trigger -> CastOffer ->
                       Terminus on stack -> resolved to P0 graveyard, {W}
                       paid (miracle path only).
  A5_order_completable every EffectZoneChoice owner batch completed via the
                       viewer interaction (select submission, exact count,
                       accepted, no stall).
  A6_creatures_bottomed POST: zero creatures on either battlefield; every
                       pre-cast creature object now in Library zone; each
                       owner's library grew by their creature count.
  A7_no_softlock       no EffectZoneChoice stall >45s; stack empty after
                       resolution; game returned to priority.
  A8_cleanup           Terminus in P0 graveyard; game proceeds.

Verdict rule:
  reproduced     iff an EffectZoneChoice batch cannot be completed
                 (no completable opportunity for >45s, or submissions
                 rejected persistently) and the game stalls there.
  not-reproduced iff the mass-order completion works on the exercised path:
                 miracle path (A3+A4+A5+A6+A7) or, when the miracle offer
                 never materialized, the normal-cast control (A5+A6+A7).
  blocked        iff A2 failed (setup never reached).

Evidence: evidence/7092/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7092.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts for this game's code only).
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
RUN_ID = os.environ.get("RUN_ID", "20260913-7092")
EVDIR = f"{BACKFILL}/evidence/7092/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TERMINUS = "terminus"
BEARS = "grizzly bears"
PLAINS = "plains"
FOREST = "forest"
LANDS = (PLAINS, FOREST)

P0_DECK = [(TERMINUS, 28), (BEARS, 12), (PLAINS, 10), (FOREST, 10)]
P1_DECK = [(BEARS, 12), (FOREST, 48)]

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


def bf_creatures(state, pid):
    # In this scenario the only creatures ever on the battlefield are
    # Grizzly Bears, so the bear list IS the creature list.
    return bf_ids(state, pid, BEARS)


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


def untapped_land_names(state, pid):
    return [lname(state, oid) for oid in untapped_lands(state, pid)]


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


def wf_pending_for(state, pid):
    d = wf_of(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def vi_spec_type(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return resp.get("type")
    spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
    return spec.get("type")


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def choice_codes(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("code"):
            out.append((d.get("role"), d.get("code"), d.get("value")))
    return out


def candidate_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi_choose(c, opp, choice, tag):
    iid = opp.get("interactionId")
    sub = {"interactionId": iid,
           "response": {"type": "choose",
                        "data": {"choiceId": choice.get("id")}}}
    say(f"[{tag}] choose iid={iid} choice={choice.get('id')} "
        f"({choice_text(choice)[:80]}) codes={choice_codes(choice)}")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120],
          "codes": choice_codes(choice)})
    await c.send_interaction(sub)


async def answer_vi_select(c, opp, choice_ids, tag, note=""):
    iid = opp.get("interactionId")
    sub = {"interactionId": iid,
           "response": {"type": "select",
                        "data": {"choiceIds": choice_ids}}}
    say(f"[{tag}] select iid={iid} n={len(choice_ids)} {note}")
    wire("interaction_submission",
         {"who": tag, "submission": sub, "note": note})
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    notes = []
    obs = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
           "life_trace": [], "order_batches": [], "stall_watch": []}
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup", "A3_miracle_offer", "A4_miracle_cast",
            "A5_order_completable", "A6_creatures_bottomed",
            "A7_no_softlock", "A8_cleanup")}

    # ---- A1 (parse check) up front, from the pinned card-data.json ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.82.0/data/"
                            f"card-data.json"))
        c = cd[TERMINUS]
        ab = (c.get("abilities") or [{}])[0]
        eff = ab.get("effect", {}) or {}
        tgt = eff.get("target", {}) or {}
        mir = None
        for kw in c.get("keywords", []) or []:
            if isinstance(kw, dict) and "Miracle" in kw:
                mir = kw["Miracle"]
        ok = (eff.get("type") == "ChangeZoneAll"
              and eff.get("destination") == "Library"
              and (eff.get("library_position") or {}).get("type") == "Bottom"
              and tgt.get("type") == "Typed"
              and "Creature" in (tgt.get("type_filters") or [])
              and isinstance(mir, dict)
              and (mir.get("shards") or []) == ["White"]
              and (mir.get("generic") or 0) == 0)
        wire("parse_check", {"effect": eff, "miracle": mir})
        ass["A1_parse"] = "passed" if ok else "failed"
        notes.append(f"A1_parse -> {ass['A1_parse']} (ChangeZoneAll->Library "
                     f"Bottom, Typed Creature, Miracle {{W}})")
    except Exception as ex:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"pre_exported": False, "mid_exported": False,
          "post_exported": False,
          "miracle_offered": False, "miracle_iid": None,
          "miracle_accepted": False, "castoffer_seen": False,
          "terminus_cast": False, "terminus_on_stack_seen": False,
          "terminus_resolved": False, "terminus_resolved_at": None,
          "terminus_oid": None, "cast_path": None,  # "miracle" | "normal"
          "normal_cast_pending": False,
          "order_batches_done": 0, "order_batches": [],
          "pre_creatures": [],  # (oid, owner, controller) at pre
          "pre_lib_counts": {},
          "stuck_observed": False, "stuck_since": None,
          "rejections": 0, "p1_bear_cast": False,
          "p0_bears_cast": 0}
    prompt_done = {}
    last_select = {}

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def snapshot_pre_creatures(state):
        out = []
        for oid, o in (state.get("objects", {}) or {}).items():
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            if (o.get("zone") == "Battlefield" and nm == BEARS):
                out.append((int(oid), o.get("owner"), o.get("controller")))
        return out

    def lib_count(state, pid):
        n = 0
        for oid, o in (state.get("objects", {}) or {}).items():
            if o.get("zone") == "Library" and o.get("owner") == pid:
                n += 1
        return n

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        key = TERMINUS if pid == 0 else BEARS
        ok = (key in hn and lands >= 2) or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands; {key}={key in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        # The engine requires exactly the owed BottomCards count in one
        # SelectCards submission (validate_bottom_selection); submitting
        # fewer is rejected and stalls the pregame.
        count = 1
        for pend in (wf_of(st).get("data", {}) or {}).get("pending", []) or []:
            if pend.get("player") == pid:
                ph = pend.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    try:
                        count = max(1, int(ph.get("count", 1)))
                    except (TypeError, ValueError):
                        count = 1
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        key = TERMINUS if pid == 0 else BEARS
        pref = [oid for oid in hand if lname(st, oid) != key]
        picks = (pref + [oid for oid in hand if oid not in pref])[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(p) for p in picks]}})
        say(f"{tag} bottoms {[lname(st, p) for p in picks]}")

    async def discard_tick(c, pid, tag, st, state):
        # DiscardToHandSize is a Select-model opportunity: submit ALL
        # required discards at once as a "select" response (a single
        # "choose" is the wrong shape and leaves the prompt pending).
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        if not wf_pending_for(state, pid):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_done:
                continue
            if vi_spec_type(opp) != "select":
                continue
            chs = vi_choices(opp)
            if not chs:
                continue
            wfd = wf_of(state).get("data", {}) or {}
            count = wfd.get("count")
            if not isinstance(count, int) or count < 1:
                count = max(0, len(hand_ids(state, pid)) - 7)

            def rank(ch):
                t = choice_text(ch).lower()
                if t == FOREST or t == PLAINS:
                    return 0
                if t == TERMINUS and pid == 0:
                    return 3
                if t == BEARS:
                    return 2
                return 1

            picks = sorted(chs, key=rank)[:count]
            if len(picks) < count:
                notes.append(f"[{tag}] discard: only {len(picks)} "
                             f"candidates for count={count}")
                continue
            say(f"[{tag}] discarding {len(picks)} to hand size: "
                f"{[choice_text(p)[:20] for p in picks]}")
            wire("discard_submit",
                 {"who": tag, "iid": iid, "count": count,
                  "picks": [choice_text(p)[:30] for p in picks]})
            await answer_vi_select(c, opp, [p.get("id") for p in picks],
                                   tag, note="discard to hand size")
            prompt_done[iid] = True
            return True
        return False

    def find_miracle_accept(chs):
        for ch in chs:
            for role, code, _v in choice_codes(ch):
                if code == "castSpellAsMiracle":
                    return ch
        return None

    def can_pay_w(state, pid):
        return PLAINS in untapped_land_names(state, pid)

    def board_ready_for_choice(state):
        # The reported softlock needs the EffectZoneChoice prompt, which the
        # engine raises only when >1 object moves. Require 2+ creatures per
        # owner so every owner makes a real ordering choice.
        c0 = len([o for o in bf_creatures(state, 0)])
        c1 = len([o for o in bf_creatures(state, 1)])
        return c0 >= 2 and c1 >= 2

    def find_miracle_decline(chs, accept):
        # Never return a choice carrying the miracle-cast action code,
        # even if choice ordering/duplication is surprising.
        def is_accept_ch(ch):
            return any(code == "castSpellAsMiracle"
                       for _, code, _v in choice_codes(ch))
        for ch in chs:
            if is_accept_ch(ch):
                continue
            for role, code, v in choice_codes(ch):
                if str(role).lower() == "accept" and str(v).lower() == "false":
                    return ch
        for ch in chs:
            if not is_accept_ch(ch):
                return ch
        return None

    async def handle_miracle(c, pid, tag, st, state):
        """Answer MiracleReveal / miracle CastOffer for seat pid.

        Accept the reveal only when {W} is payable right now (an untapped
        Plains); otherwise decline so the game keeps moving and a later
        offer (with mana up) becomes the real test. A CastOffer with no
        accept candidate is declined the same way instead of spinning.
        """
        wtype = wf_of(state).get("type") or ""
        if wtype not in ("MiracleReveal", "CastOffer"):
            return False
        if not wf_pending_for(state, pid):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_done:
                continue
            chs = vi_choices(opp)
            if not chs:
                continue
            wire("miracle_choices_debug",
                 {"wtype": wtype, "iid": iid,
                  "choices": [{"id": ch.get("id"),
                               "text": choice_text(ch)[:60],
                               "codes": choice_codes(ch)} for ch in chs]})
            accept = find_miracle_accept(chs)
            if wtype == "MiracleReveal":
                ST["miracle_offered"] = True
                board = board_ready_for_choice(state)
                if accept is not None and can_pay_w(state, pid) and board:
                    if not ST["pre_exported"]:
                        await export_named("pre")
                        ST["pre_exported"] = True
                        ST["pre_creatures"] = snapshot_pre_creatures(state)
                        ST["pre_lib_counts"] = {0: lib_count(state, 0),
                                                1: lib_count(state, 1)}
                        ST["cast_path"] = "miracle"
                    ST["miracle_accepted"] = True
                    say(f"[{tag}] accepting miracle reveal "
                        f"(pre creatures={ST['pre_creatures']})")
                    await answer_vi_choose(c, opp, accept, tag)
                    prompt_done[iid] = True
                    return True
                decline = find_miracle_decline(chs, accept)
                if decline is None:
                    continue
                if not can_pay_w(state, pid):
                    reason = "cannot pay {W}"
                    ST["declined_unpayable"] = ST.get("declined_unpayable", 0) + 1
                else:
                    reason = "board not ready (<2 creatures per owner)"
                    ST["declined_small_board"] = \
                        ST.get("declined_small_board", 0) + 1
                ST["pre_exported"] = False  # re-arm: next accept re-exports
                ST["pre_creatures"] = []
                ST["cast_path"] = None
                say(f"[{tag}] declining miracle reveal ({reason}; "
                    f"creatures p0={len(bf_creatures(state, 0))} "
                    f"p1={len(bf_creatures(state, 1))} "
                    f"untapped={untapped_land_names(state, pid)})")
                wire("miracle_declined_unpayable",
                     {"untapped": untapped_land_names(state, pid)})
                await answer_vi_choose(c, opp, decline, tag)
                prompt_done[iid] = True
                return True
            # wtype == "CastOffer"
            # Gate the accept exactly like the reveal accept: board ready,
            # {W} payable, pre exported. (Run 20260913-7092e accepted a
            # CastOffer blindly on an empty board: the cast resolved with no
            # creatures, no EffectZoneChoice was ever raised, and the run
            # was inconclusive with pre.json missing.)
            if accept is not None:
                board = board_ready_for_choice(state)
                if can_pay_w(state, pid) and board:
                    if not ST["pre_exported"]:
                        await export_named("pre")
                        ST["pre_exported"] = True
                        ST["pre_creatures"] = snapshot_pre_creatures(state)
                        ST["pre_lib_counts"] = {0: lib_count(state, 0),
                                                1: lib_count(state, 1)}
                        ST["cast_path"] = "miracle"
                    ST["castoffer_seen"] = True
                    ST["miracle_accepted"] = True
                    ST["terminus_cast"] = True
                    say(f"[{tag}] accepting miracle CastOffer "
                        f"(pre creatures={ST['pre_creatures']})")
                    wire("miracle_castoffer_accept", {"iid": iid})
                    await answer_vi_choose(c, opp, accept, tag)
                    prompt_done[iid] = True
                    return True
                say(f"[{tag}] declining miracle CastOffer (board not ready "
                    f"or {{W}} unpayable; creatures p0="
                    f"{len(bf_creatures(state, 0))} p1="
                    f"{len(bf_creatures(state, 1))} "
                    f"untapped={untapped_land_names(state, pid)})")
                wire("miracle_castoffer_declined_notready",
                     {"untapped": untapped_land_names(state, pid)})
                decline = find_miracle_decline(chs, accept)
                if decline is not None:
                    await answer_vi_choose(c, opp, decline, tag)
                    prompt_done[iid] = True
                    return True
                continue
            decline = find_miracle_decline(chs, accept)
            if decline is None:
                notes.append(f"[{tag}] CastOffer: no accept and no "
                             f"decline choice; choices="
                             f"{[choice_text(x)[:30] for x in chs]}")
                wire("miracle_castoffer_no_choices",
                     {"choices": [choice_text(x)[:40] for x in chs]})
                continue
            ST["declined_unpayable"] = ST.get("declined_unpayable", 0) + 1
            ST["pre_exported"] = False
            ST["pre_creatures"] = []
            ST["cast_path"] = None
            ST["miracle_accepted"] = False
            say(f"[{tag}] declining miracle CastOffer (accept not offered)")
            wire("miracle_castoffer_declined", {})
            await answer_vi_choose(c, opp, decline, tag)
            prompt_done[iid] = True
            return True
        return False

    async def handle_order_choice(c, pid, tag, st, state):
        """Complete a mass-library-order EffectZoneChoice batch for pid."""
        if (wf_of(state).get("type") or "") != "EffectZoneChoice":
            return False
        if not wf_pending_for(state, pid):
            return False
        vi = get_vi(st)
        if not vi:
            obs["stall_watch"].append(
                {"t": time.time(), "who": tag, "note": "EffectZoneChoice "
                 "pending but no viewer interaction for seat"})
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_done:
                continue
            spec = vi_spec_type(opp)
            chs = vi_choices(opp)
            if spec != "select" or not chs:
                obs["stall_watch"].append(
                    {"t": time.time(), "who": tag,
                     "note": f"EffectZoneChoice opp spec={spec} "
                             f"candidates={len(chs)} (not completable)"})
                say(f"[{tag}] EffectZoneChoice opp not completable: "
                    f"spec={spec} candidates={len(chs)}")
                continue
            if not ST["mid_exported"]:
                await export_named("mid")
                ST["mid_exported"] = True
            # order = advertised candidate order (deterministic);
            # record the oid order actually submitted.
            cids = [ch.get("id") for ch in chs]
            oids = [candidate_ref(ch) for ch in chs]
            names = [lname(state, r) if isinstance(r, int) else "?"
                     for r in oids]
            batch = {"who": tag, "iid": iid, "n": len(cids),
                     "submitted_oids": oids, "submitted_names": names}
            ST["order_batches"].append(batch)
            obs["order_batches"].append(batch)
            wire("order_batch_submit", batch)
            say(f"[{tag}] answering EffectZoneChoice batch: n={len(cids)} "
                f"names={names}")
            await answer_vi_select(c, opp, cids, tag,
                                   note=f"mass library order batch ({tag})")
            prompt_done[iid] = True
            ST["order_batches_done"] += 1
            return True
        return False

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        try:
            if wtype == "MulliganDecision":
                if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                    await do_mulligan(p0, 0, "P0")
                    return
                if (find_action(acts, "SelectCards")
                        and last_select.get(0) != p0.revision):
                    last_select[0] = p0.revision
                    await do_bottom(p0, 0, "P0")
                    return
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(p0, a)
                    return
            if wtype in ("DeclareAttackers", "DeclareBlockers"):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    if wtype == "DeclareAttackers":
                        # zero attackers: keep both boards intact for the proof
                        sub["data"]["attacks"] = []
                        sub["data"]["bands"] = []
                    else:
                        sub["data"]["assignments"] = []
                    await submit_as_is(p0, sub)
                    return
            # miracle path first
            if await handle_miracle(p0, 0, "P0", st, state):
                return
            if await handle_order_choice(p0, 0, "P0", st, state):
                return
            # stall watchdog on EffectZoneChoice
            if wtype == "EffectZoneChoice" and wf_pending_for(state, 0):
                if ST["stuck_since"] is None:
                    ST["stuck_since"] = time.time()
                    say("[P0] EffectZoneChoice pending with no completable "
                        "opportunity yet; starting stall clock")
                elif time.time() - ST["stuck_since"] > 45:
                    ST["stuck_observed"] = True
                    obs["stall_watch"].append(
                        {"t": time.time(), "who": "P0",
                         "note": "STUCK >45s at EffectZoneChoice"})
                    say("[P0] STUCK at EffectZoneChoice >45s -> reproduced")
                    wire("stuck_observed",
                         {"wf": wf_of(state), "turn": turn, "phase": phase})
                    await export_named("post")
                    ST["post_exported"] = True
                    await finish()
                    return
            else:
                ST["stuck_since"] = None
            # Terminus on stack / resolution tracking
            if ST["terminus_cast"] and not ST["terminus_resolved"]:
                on_stack = any(
                    lname(state, int(oid)) == TERMINUS
                    for oid, o in (state.get("objects", {}) or {}).items()
                    if o.get("zone") == "Stack" and o.get("controller") == 0)
                if on_stack and not ST["terminus_on_stack_seen"]:
                    ST["terminus_on_stack_seen"] = True
                    say("Terminus seen on the stack")
                    wire("terminus_on_stack", {})
                if ST["terminus_on_stack_seen"] and not on_stack:
                    in_gy = any(
                        lname(state, int(oid)) == TERMINUS
                        for oid, o in (state.get("objects", {}) or {}).items()
                        if o.get("zone") == "Graveyard"
                        and o.get("controller") == 0)
                    ST["terminus_resolved"] = True
                    ST["terminus_resolved_at"] = time.time()
                    say(f"Terminus resolved (in P0 gy={in_gy})")
                    wire("terminus_resolved", {"in_P0_gy": in_gy})
            if (ST["terminus_resolved"] and not ST["post_exported"]
                    and (time.time() - (ST["terminus_resolved_at"] or 0)) > 10
                    and my_priority(state, 0)):
                await export_named("post")
                ST["post_exported"] = True
                await finish()
                return
            # fallback: normal cast for {4}{W}{W} if no miracle cast yet by
            # P0 turn 12. Gated on miracle_accepted (not miracle_offered):
            # declined offers must not block the control path.
            if state.get("active_player") == 0:
                last_t = ST.get("last_turn_seen")
                if last_t != turn:
                    ST["last_turn_seen"] = turn
                    ST["p0_turns"] = ST.get("p0_turns", 0) + 1
            p0_turns = ST.get("p0_turns", 0)
            if (not ST["miracle_accepted"] and not ST["terminus_cast"]
                    and p0_turns >= 12
                    and phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and TERMINUS in hand_lnames(state, 0)):
                ul = untapped_land_names(state, 0)
                if len(ul) >= 6 and ul.count(PLAINS) >= 2:
                    for a in acts:
                        if a.get("type") != "CastSpell":
                            continue
                        d = a.get("data", {}) or {}
                        oid = d.get("object_id")
                        if (isinstance(oid, int)
                                and lname(state, oid) == TERMINUS):
                            if not ST["pre_exported"]:
                                await export_named("pre")
                                ST["pre_exported"] = True
                                ST["pre_creatures"] = snapshot_pre_creatures(state)
                                ST["pre_lib_counts"] = {
                                    0: lib_count(state, 0),
                                    1: lib_count(state, 1)}
                            ST["terminus_oid"] = oid
                            ST["cast_path"] = "normal"
                            ST["terminus_cast"] = True
                            ST["normal_cast_pending"] = True
                            say(f"[P0] NORMAL-CAST Terminus oid={oid} "
                                f"(fallback, turn {turn})")
                            wire("normal_cast", {"oid": oid})
                            await submit_as_is(p0, a)
                            return
            if await discard_tick(p0, 0, "P0", st, state):
                return
            # land drops + bears
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and not ST["terminus_cast"]):
                la = find_action(acts, "PlayLand")
                if la:
                    await submit_as_is(p0, la)
                    return
                if len(untapped_lands(state, 0)) >= 2 \
                        and len(bf_creatures(state, 0)) < 3 \
                        and FOREST in untapped_land_names(state, 0):
                    for a in acts:
                        if a.get("type") != "CastSpell":
                            continue
                        d = a.get("data", {}) or {}
                        oid = d.get("object_id")
                        if (isinstance(oid, int)
                                and lname(state, oid) == BEARS):
                            await submit_as_is(p0, a)
                            ST["p0_bears_cast"] += 1
                            say(f"[P0] casts Grizzly Bears oid={oid}")
                            return
            if my_priority(state, 0):
                pa = find_action(acts, "PassPriority")
                if pa:
                    await submit_as_is(p0, pa)
                    return
        except Exception as e:
            obs["tick_errors"].append(f"P0: {e!r}")
            say(f"[P0] tick error: {e!r}")

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        try:
            if wtype == "MulliganDecision":
                if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                    await do_mulligan(p1, 1, "P1")
                    return
                if (find_action(acts, "SelectCards")
                        and last_select.get(1) != p1.revision):
                    last_select[1] = p1.revision
                    await do_bottom(p1, 1, "P1")
                    return
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(p1, a)
                    return
            if wtype == "DeclareAttackers" and wf_pending_for(state, 1):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    # zero attackers: keep both boards intact for the proof
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                    await submit_as_is(p1, sub)
                    return
            if wtype == "DeclareBlockers" and wf_pending_for(state, 1):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    sub["data"]["assignments"] = []
                    await submit_as_is(p1, sub)
                    return
            if await handle_order_choice(p1, 1, "P1", st, state):
                return
            if wtype == "EffectZoneChoice" and wf_pending_for(state, 1):
                if ST["stuck_since"] is None:
                    ST["stuck_since"] = time.time()
                elif time.time() - ST["stuck_since"] > 45:
                    ST["stuck_observed"] = True
                    say("[P1] STUCK at EffectZoneChoice >45s -> reproduced")
                    wire("stuck_observed",
                         {"wf": wf_of(state)})
                    await export_named("post")
                    ST["post_exported"] = True
                    await finish()
                    return
            else:
                if (wf_of(state).get("type") or "") != "EffectZoneChoice":
                    ST["stuck_since"] = None
            if await discard_tick(p1, 1, "P1", st, state):
                return
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 1
                    and my_priority(state, 1)
                    and not ST["terminus_cast"]):
                la = find_action(acts, "PlayLand")
                if la:
                    await submit_as_is(p1, la)
                    return
                if len(untapped_lands(state, 1)) >= 2 \
                        and len(bf_creatures(state, 1)) < 3:
                    for a in acts:
                        if a.get("type") != "CastSpell":
                            continue
                        d = a.get("data", {}) or {}
                        oid = d.get("object_id")
                        if (isinstance(oid, int)
                                and lname(state, oid) == BEARS):
                            await submit_as_is(p1, a)
                            ST["p1_bear_cast"] = True
                            say(f"[P1] casts Grizzly Bears oid={oid}")
                            return
            if my_priority(state, 1):
                pa = find_action(acts, "PassPriority")
                if pa:
                    await submit_as_is(p1, pa)
                    return
        except Exception as e:
            obs["tick_errors"].append(f"P1: {e!r}")
            say(f"[P1] tick error: {e!r}")

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

        # ---- A2: setup (from pre.json) ----
        if pre is not None:
            own0 = [c for c in ST["pre_creatures"] if c[1] == 0]
            own1 = [c for c in ST["pre_creatures"] if c[1] == 1]
            ok = (len(own0) >= 1 and len(own1) >= 1
                  and ST["cast_path"] in ("miracle", "normal"))
            notes.append(f"A2: pre creatures P0-owned={len(own0)} "
                         f"P1-owned={len(own1)} path={ST['cast_path']}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: miracle offer ----
        if ST["miracle_offered"]:
            ass["A3_miracle_offer"] = "passed"
            notes.append(f"A3: MiracleReveal raised "
                         f"(declined_unpayable="
                         f"{ST.get('declined_unpayable', 0)}; accepted="
                         f"{ST['miracle_accepted']})")
        elif ST["cast_path"] == "normal":
            ass["A3_miracle_offer"] = "not-run"
            notes.append("A3 not-run: no miracle offer materialized by "
                         "P0 turn 12 (RNG); normal-cast control exercised")
        else:
            ass["A3_miracle_offer"] = "failed"
            notes.append("A3 failed: no miracle offer and no normal cast")

        # ---- A4: miracle cast resolves ----
        if ST["cast_path"] == "miracle":
            ok = ST["terminus_resolved"]
            ass["A4_miracle_cast"] = "passed" if ok else "failed"
            notes.append(f"A4: miracle_accepted={ST['miracle_accepted']} "
                         f"castoffer={ST['castoffer_seen']} "
                         f"on_stack={ST['terminus_on_stack_seen']} "
                         f"resolved={ST['terminus_resolved']}")
        else:
            ass["A4_miracle_cast"] = "not-run"
            notes.append("A4 not-run: miracle path not taken")

        # ---- A5: order batches completable ----
        if ST["order_batches_done"] >= 1 and not ST["stuck_observed"]:
            ass["A5_order_completable"] = "passed"
        elif ST["stuck_observed"]:
            ass["A5_order_completable"] = "failed"
        else:
            ass["A5_order_completable"] = ("not-run"
                                           if not ST["terminus_resolved"]
                                           else "failed")
        notes.append(f"A5: batches_done={ST['order_batches_done']} "
                     f"stuck={ST['stuck_observed']} "
                     f"rejections={ST['rejections']}")

        # ---- A6: creatures bottomed ----
        if post is not None and pre is not None:
            bf_creatures = [oid for oid, o in (post.get("objects", {})
                                               or {}).items()
                            if o.get("zone") == "Battlefield"
                            and str(o.get("base_name") or o.get("name")
                                    or "").lower() == BEARS]
            moved = 0
            for oid, owner, _ctl in ST["pre_creatures"]:
                o = (post.get("objects", {}) or {}).get(str(oid), {})
                if o.get("zone") == "Library":
                    moved += 1
                else:
                    notes.append(f"A6: creature oid={oid} owner={owner} "
                                 f"ended in zone={o.get('zone')}")
            lib0 = lib_count(post, 0) - ST["pre_lib_counts"].get(0, 0)
            lib1 = lib_count(post, 1) - ST["pre_lib_counts"].get(1, 0)
            exp0 = len([c for c in ST["pre_creatures"] if c[1] == 0])
            exp1 = len([c for c in ST["pre_creatures"] if c[1] == 1])
            ok = (not bf_creatures and moved == len(ST["pre_creatures"])
                  and lib0 == exp0 and lib1 == exp1)
            notes.append(f"A6: bf_creatures_post={len(bf_creatures)} "
                         f"moved_to_library={moved}/{len(ST['pre_creatures'])} "
                         f"lib_delta=[{lib0},{lib1}] expected=[{exp0},{exp1}]")
            ass["A6_creatures_bottomed"] = "passed" if ok else "failed"
        else:
            ass["A6_creatures_bottomed"] = "failed"
            notes.append("A6 failed: pre/post state missing")

        # ---- A7: no softlock ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            ok = (not ST["stuck_observed"] and stack_empty
                  and wft in ("Priority",))
            notes.append(f"A7: stuck={ST['stuck_observed']} "
                         f"stack_empty={stack_empty} post_wf={wft}")
            ass["A7_no_softlock"] = "passed" if ok else "failed"
        else:
            ass["A7_no_softlock"] = "failed"
            notes.append("A7 failed: post.json missing")

        # ---- A8: cleanup ----
        if post is not None:
            tgy = any(lname(post, int(oid)) == TERMINUS
                      for oid, o in (post.get("objects", {}) or {}).items()
                      if o.get("zone") == "Graveyard"
                      and o.get("controller") == 0)
            ass["A8_cleanup"] = "passed" if tgy else "failed"
            notes.append(f"A8: Terminus in P0 gy={tgy}")
        else:
            ass["A8_cleanup"] = "failed"
            notes.append("A8 failed: post.json missing")

        # ---- verdict ----
        if ST["stuck_observed"]:
            verdict = "reproduced"
            notes.append("verdict=reproduced: EffectZoneChoice could not be "
                         "completed; game stalled >45s at the mass "
                         "library-order decision")
        elif ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif (ass["A5_order_completable"] == "passed"
                and ass["A6_creatures_bottomed"] == "passed"
                and ass["A7_no_softlock"] == "passed"):
            verdict = "not-reproduced"
            notes.append(f"verdict=not-reproduced: {ST['cast_path']} cast "
                         "completed every owner batch of the mass "
                         "library-order choice; all creatures reached the "
                         "bottom of their owners' libraries; no stall")
        elif ass["A5_order_completable"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: order batches not completable")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7092,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh by this run; "
                               "its games.db holds only this run)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7092.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7092.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "28x Terminus / 12x Grizzly Bears density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "Miracle path depends on RNG: Terminus must be the first "
                "card drawn in a turn. If no miracle cast materializes by P0 "
                "turn 12, a normal-cast control exercises the same mass-order "
                "completion path.",
                "Bottom-of-library ORDER is submitted per CR 401.4 and "
                "recorded; zone/count assertions verify placement, not "
                "exact stack positions within the library list.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7092.py",
                    f"{EVDIR}/scenario_7092.py")
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{RUN_ID}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            excerpt = [ln for ln in clean.splitlines() if GAME in ln]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines for "
                f"game {GAME})")
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
        d.text((24, y), "phase-rs/phase #7092 - Terminus / EffectZoneChoice",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-13 - "
               "mass library-order completion",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: put all creatures on the bottom of their "
               "owners' libraries. Miracle {W}.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: ChangeZoneAll->Library/Bottom supported",
            "A2_setup": "PRE: creatures owned by both players on BF",
            "A3_miracle_offer": "MiracleReveal raised for first-draw",
            "A4_miracle_cast": "miracle cast completes ({W} paid)",
            "A5_order_completable": "every owner batch completable via UI",
            "A6_creatures_bottomed": "all creatures -> owners' libraries",
            "A7_no_softlock": "no stall; stack empty; priority returns",
            "A8_cleanup": "Terminus in P0 graveyard",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), f"cast path: {run['driver_state'].get('cast_path')}; "
               f"batches done: "
               f"{run['driver_state'].get('order_batches_done')}; "
               f"stuck: {run['driver_state'].get('stuck_observed')}",
               fill=(200, 210, 225))
        y += 28
        d.text((24, y), "Board across states:", fill=(200, 210, 225))
        y += 24
        for label in ("pre", "mid", "post"):
            st = states.get(label)
            if st is not None:
                nb = sum(1 for o in (st.get("objects", {}) or {}).values()
                         if o.get("zone") == "Battlefield"
                         and str(o.get("base_name") or o.get("name")
                                 or "").lower() == BEARS)
                line = (f"{label:>4}: life {life_of(st, 0)}/{life_of(st, 1)}  "
                        f"bears_on_bf={nb}  wf="
                        f"{(st.get('waiting_for') or {}).get('type')}  "
                        f"stack={len(st.get('stack') or [])}")
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:14]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_7092.py", "wire_log.jsonl", "scenario_run.log",
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
        while time.time() - t0 < 600:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"DIAG turn={s.get('turn_number')} "
                    f"active={s.get('active_player')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"pp={s.get('priority_player')} "
                    f"life={[life_of(s, i) for i in (0, 1)]} "
                    f"miracle={ST['miracle_offered']} "
                    f"path={ST['cast_path']} "
                    f"batches={ST['order_batches_done']} "
                    f"resolved={ST['terminus_resolved']} "
                    f"stuck={ST['stuck_observed']}")
            if (s.get("turn_number") or 0) > 30 and not ST["terminus_cast"]:
                notes.append("watchdog: turn 30 with no cast; finishing")
                break
        if not ST["post_exported"]:
            notes.append("global timeout (600s) hit before post export")
    finally:
        p0t.cancel()
        p1t.cancel()
    await finish()


asyncio.run(main())
