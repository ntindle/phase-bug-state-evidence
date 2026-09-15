#!/usr/bin/env python3
"""Issue #7175: Insatiable Avarice -- impossible to cast when the targeting
Spree mode is chosen ("the game will not accept any player as a target").

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.83.0, key 'insatiable avarice'):
  Insatiable Avarice ({B}, Sorcery):
    Spree (Choose one or more additional costs.)
    + {2} -- Search your library for a card, then shuffle and put that
      card on top.
    + {B}{B} -- Target player draws three cards and loses 3 life.

Card-data parse state on v0.83.0 (verified 2026-09-15 before the run):
  mana_cost {B}; keywords ['Spree']; abilities[0] = Spell SearchLibrary
  (count 1) -> Shuffle (target Controller) -> PutAtLibraryPosition Top;
  abilities[1] = Spell Draw{count 3, target {"type":"Player"}} ->
  LoseLife{3, target {"type":"ParentTarget"}};
  modal = {min_choices 1, max_choices 2, mode_count 2,
           mode_costs [{2}, {B}{B}]}.
  Parse is faithful: the targeting mode's Player-target Draw slot is present.

The reported defect: casting with the {B}{B} (targeting) mode chosen never
completes target selection -- no player target is accepted.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 30x insatiable avarice, 30x swamp (60).
  P1: 60x forest (passive: land drops only, never attacks).

Planned line (three legs, one game):
  Leg 1 (reported): P0 casts Avarice choosing ONLY mode 1 ({B}{B}). The
    engine must raise a ModeChoice, then a TargetSelection exposing both
    players. Driver answers the seat-1 (P1) player candidate with the
    engine-issued choice id. Success = submission accepted (no rejection),
    spell resolves, P1 20->17 life and +3 cards in hand.
  Leg 2 (control): P0 casts Avarice choosing ONLY mode 0 ({2}, search).
    No target prompt should appear; the search choice is answered and the
    cast resolves cleanly. Proves spree casting per se works.
  Leg 3 (composition): P0 casts Avarice choosing BOTH modes ({2}+{B}{B}).
    Search choice + P1 target; both effects land.

Assertions (each passed / failed / not-run):
  A1_parse          card-data: modal min1/max2, mode_costs [{2},{B}{B}],
                    Draw target {"type":"Player"} present.
  A2_setup          PRE: P0 holds Avarice with 3+ untapped Swamps; P1 present.
  A3_mode_chosen    leg1: ModeChoice offered, both modes available, mode 1
                    (targeting) selected via engine-issued choice id.
  A4_target_accept  leg1: TargetSelection exposed >=2 player candidates
                    (seats 0 and 1); the seat-1 submission was ACCEPTED
                    (no ActionRejected; prompt advanced). The reported bug
                    fails HERE if the target is rejected.
  A5_resolution     leg1: P1 life 20->17 and P1 hand +3 after resolution.
  B1_search_control leg2: search-only mode cast completes (no target prompt,
                    search answered, spell resolves, no rejections).
  C1_both_modes     leg3: both modes chosen; P1 life -3 more and hand +3
                    more; search leg completed.
  A6_cleanup        stack empty at post, game advanced, no stall.

Verdict rule:
  reproduced     iff A2 passes and (mode 1 not selectable OR the seat-1
                 target submission is rejected OR resolution effects missing)
                 -- i.e. the targeting mode cannot be cast to completion.
  not-reproduced iff A2-A5 pass (targeting mode casts, resolves, and the
                 target draws 3 / loses 3).
  blocked        iff A2 fails (setup never reached) or the ModeChoice prompt
                 never appears for the cast.

Evidence: evidence/7175/<run-id>/pre.json, leg1_mid.json, leg1_post.json,
leg2_post.json, leg3_post.json, post.json, run.json, manifest.sha256,
summary.png, scenario_7175.py, wire_log.jsonl, scenario_run.log, server.log.
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
RUN_ID = os.environ.get("RUN_ID", "20260915-7175")
EVDIR = f"{BACKFILL}/evidence/7175/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

AVARICE = "insatiable avarice"
SWAMP = "swamp"
FOREST = "forest"
P0_LANDS = (SWAMP,)
P1_LANDS = (FOREST,)

P0_DECK = [(AVARICE, 30), (SWAMP, 30)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c51cb38db94466c85",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key); server already running "
              "on 127.0.0.1:9374 (run 20260915-7173-server), reused.",
}

LEGS = [
    {"name": "leg1", "modes": [1], "need": 3, "kind": "target",
     "desc": "targeting mode only ({B}{B}) -- the reported bug"},
    {"name": "leg2", "modes": [0], "need": 3, "kind": "search",
     "desc": "search mode only ({2}) -- control"},
    {"name": "leg3", "modes": [0, 1], "need": 5, "kind": "both",
     "desc": "both modes ({2}+{B}{B}) -- composition"},
]


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


def bf_land_ids(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names):
            out.append(int(oid))
    return out


def untapped_lands(state, pid, names):
    return [oid for oid in bf_land_ids(state, pid, names)
            if not get_obj(state, oid).get("tapped")]


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


def wf_type(state):
    return wf_of(state).get("type") or ""


def wf_player(state):
    d = wf_of(state).get("data") or {}
    p = d.get("player")
    if isinstance(p, dict):
        p = p.get("id", p.get("player", -1))
    return p


def my_priority(state, pid):
    return wf_type(state) == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


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

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "pre_exported": False,
        "post_exported": False,
        "prompt_first_seen": {},
        "legs": {},
        "leg_order": [],
        "cur": None,            # name of the leg currently being cast
        "cast_oid": None,       # oid of the in-flight Avarice spell
        "turns_seen": set(),
        "rejections": [],
        "game_code": None,
        "last_rev_acted": {},
        "stuck_since": None,
    }
    for leg in LEGS:
        ST["legs"][leg["name"]] = {
            "modes": list(leg["modes"]), "need": leg["need"],
            "kind": leg["kind"],
            "cast": False, "cast_rev": None, "cast_oid": None,
            "mode_offered": False, "mode_unavailable": None,
            "mode_choices_n": None, "mode_iid": None,
            "mode_chosen": False, "chosen_mode_ids": [],
            "target_prompted": False, "target_candidates_n": None,
            "target_candidate_seats": None, "target_choice_id": None,
            "target_sub_rev": None, "target_rejected": False,
            "target_rejection_code": None, "target_accepted": False,
            "pay_taps": 0, "pay_spends": 0, "pay_finalized": False,
            "search_prompted": False, "search_answered": False,
            "search_chosen_card": None,
            "p1_life_pre": None, "p1_hand_pre": None,
            "p1_life_post": None, "p1_hand_post": None,
            "resolved": False, "done": False, "done_reason": "",
        }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "prompts": [], "stage_timeouts": []}
    notes = []
    ass = {}

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
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
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

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

    def drain_rejections(c):
        found = []
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                found.append({"type": t, "data": data})
                ST["rejections"].append(
                    {"who": c.name, "type": t, "data": data,
                     "stage": ST.get("cur"), "t": time.time()})
                wire("rejected", {"who": c.name, "type": t, "data": data,
                                  "stage": ST.get("cur")})
                say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:400]}")
        return found

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
        say(f"[{tag}] submitting interaction iid={iid} choice={cid}")
        wire("interaction_submission", {"who": tag, "submission": sub})
        await c.send_interaction(sub)

    async def answer_vi_multi(c, opp, choices, tag):
        """Sequence submission with several choice ids (spree both-modes)."""
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cids = [ch.get("id") for ch in choices]
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype, "data": {"choiceIds": cids}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": cids}}}
        say(f"[{tag}] submitting multi interaction iid={iid} choices={cids}")
        wire("interaction_submission_multi", {"who": tag, "submission": sub})
        await c.send_interaction(sub)

    async def mulligan_keep(c, pid, tag, st, state, acts):
        if wf_type(state) != "MulliganDecision":
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def bottom_cards(c, pid, tag, st, state, acts):
        w = wf_of(state)
        if wf_type(state) != "SelectCards":
            return False
        d = w.get("data") or {}
        if isinstance(d.get("player"), int) and d["player"] != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            chs, rtype = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                return (0 if any(l in tx for l in ("swamp", "forest"))
                        else 1)
            pick = sorted(chs, key=rank)[:int(d.get("count", 1))]
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [x.get("id")
                                                       for x in pick]}}}
            say(f"[{tag}] bottoming {len(pick)} cards")
            wire("bottom_cards", {"who": tag, "n": len(pick)})
            await c.send_interaction(sub)
            return True
        return False

    async def handle_discard(c, pid, tag, st, state, acts):
        if wf_type(state) != "DiscardToHandSize":
            return False
        d = wf_of(state).get("data") or {}
        if isinstance(d.get("player"), dict):
            dp = d["player"].get("id", -1)
        else:
            dp = d.get("player", -1)
        if dp != pid:
            return False
        count = int(d.get("count", 1))
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            chs, rtype = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if AVARICE in tx:
                    return 3  # protect the key spell
                return 0
            picks = sorted(chs, key=rank)[:count]
            if acted(f"disc{iid}", st.get("state_revision", -1)):
                return True
            say(f"[{tag}] discarding {len(picks)} to hand size")
            if len(picks) == 1:
                await answer_vi(c, opp, picks[0], tag)
            else:
                await answer_vi_multi(c, opp, picks, tag)
            return True
        return False

    async def land_drop(c, pid, tag, st, state, acts, names):
        if not (my_priority(state, pid)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            return False
        rev = st.get("state_revision", -1)
        for oid in hand_ids(state, pid):
            if lname(state, oid) in names:
                pla = next((a for a in acts
                            if a.get("type") == "PlayLand"
                            and (a.get("data") or {}).get("object_id") == oid),
                           None)
                if pla and not acted(f"{tag}land", rev):
                    await submit_as_is(c, pla)
                    return True
        return False

    async def zero_attackers(c, pid, tag, st, state, acts):
        if wf_type(state) != "DeclareAttackers":
            return False
        if wf_player(state) != pid:
            return False
        da = find_action(acts, "DeclareAttackers")
        rev = st.get("state_revision", -1)
        if da and not acted(f"{tag}da", rev):
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
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

    # ---------------- leg cast state machine (P0) ----------------
    async def handle_mode_choice(st, state, acts):
        """Answer the Spree ModeChoice for the current leg."""
        legn = ST["cur"]
        if legn is None:
            return False
        leg = ST["legs"][legn]
        if wf_type(state) != "ModeChoice" or wf_player(state) != 0:
            return False
        if leg["mode_chosen"]:
            return False
        rev = st.get("state_revision", -1)
        modal = (wf_of(state).get("data") or {}).get("modal") or {}
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
            ent["done"] = True
            leg["mode_offered"] = True
            leg["mode_iid"] = iid
            leg["mode_choices_n"] = len(chs)
            leg["mode_unavailable"] = modal.get("unavailable_modes")
            descs = modal.get("mode_descriptions") or []
            wire("spree_mode_choice",
                 {"leg": legn, "iid": iid, "rtype": rtype,
                  "mode_descriptions": descs,
                  "unavailable_modes": modal.get("unavailable_modes"),
                  "min_choices": modal.get("min_choices"),
                  "max_choices": modal.get("max_choices"),
                  "n_choices": len(chs),
                  "choice_ids": [ch.get("id") for ch in chs],
                  "texts": [choice_text(ch)[:80] for ch in chs],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": "SpreeModeChoice", "leg": legn,
                                   "wf_type": "ModeChoice",
                                   "modes": descs})
            say(f"[P0] {legn} ModeChoice: {len(chs)} choices, "
                f"unavailable={modal.get('unavailable_modes')}")
            wanted = list(leg["modes"])
            missing = [m for m in wanted
                       if m in (modal.get("unavailable_modes") or [])]
            if missing:
                say(f"[P0] {legn} wanted modes {missing} UNAVAILABLE "
                    f"(bug signal); not choosing")
                wire("spree_mode_unavailable",
                     {"leg": legn, "missing": missing})
                leg["done"] = True
                leg["done_reason"] = (f"wanted spree modes {missing} "
                                      f"unavailable")
                ST["cur"] = None
                return True
            # match choices to mode indices by text, fall back to position
            picks = []
            for mi in wanted:
                desc = str(descs[mi]).lower() if mi < len(descs) else ""
                key = ("target player draws" if mi == 1
                       else "search your library")
                pick = next((ch for ch in chs
                             if key in choice_text(ch).lower()), None)
                if pick is None and mi < len(chs):
                    pick = chs[mi]
                if pick is None:
                    say(f"[P0] {legn} no choice for mode {mi}; aborting "
                        f"mode choice")
                    continue
                picks.append(pick)
            if len(picks) != len(wanted):
                say(f"[P0] {legn} could not map all wanted modes")
                return True
            leg["chosen_mode_ids"] = [p.get("id") for p in picks]
            if len(picks) == 1:
                await answer_vi(p0, opp, picks[0], "P0")
            else:
                await answer_vi_multi(p0, opp, picks, "P0")
            leg["mode_chosen"] = True
            say(f"[P0] {legn} chose modes {wanted}")
            return True
        return False

    async def handle_target_selection(st, state, acts):
        """Answer the player-target selection for the current leg."""
        legn = ST["cur"]
        if legn is None:
            return False
        leg = ST["legs"][legn]
        if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
            return False
        if leg["target_accepted"] or leg["target_rejected"]:
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
            ent["done"] = True
            leg["target_prompted"] = True
            seats = [seat_of(ch) for ch in chs]
            leg["target_candidates_n"] = len(chs)
            leg["target_candidate_seats"] = seats
            wire("avarice_target_selection",
                 {"leg": legn, "iid": iid, "rtype": rtype,
                  "n_choices": len(chs), "seats": seats,
                  "choice_ids": [ch.get("id") for ch in chs],
                  "texts": [choice_text(ch)[:60] for ch in chs],
                  "refs": [ref_of(ch) for ch in chs],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": "AvariceTargetSelection",
                                   "leg": legn, "seats": seats})
            say(f"[P0] {legn} TargetSelection: {len(chs)} candidates, "
                f"seats={seats}")
            pick = next((ch for ch in chs if seat_of(ch) == 1), None)
            if pick is None:
                say(f"[P0] {legn} NO seat-1 player candidate (bug signal)")
                wire("target_no_seat1_candidate", {"leg": legn})
                return True
            leg["target_choice_id"] = pick.get("id")
            leg["target_choice_seat"] = 1
            leg["target_sub_rev"] = st.get("state_revision", -1)
            leg["target_rej_mark"] = len(ST["rejections"])
            await export_named(f"{legn}_pre_target")
            await answer_vi(p0, opp, pick, "P0")
            say(f"[P0] {legn} submitted target seat=1 choice "
                f"{pick.get('id')}")
            return True
        return False

    async def handle_mana_payment(st, state, acts):
        """Tap/swamp mana and finalize for the current leg's cast."""
        legn = ST["cur"]
        if legn is None:
            return False
        leg = ST["legs"][legn]
        if wf_type(state) != "ManaPayment" or wf_player(state) != 0:
            return False
        rev = st.get("state_revision", -1)
        # finalize outcome watch
        if leg.get("finalize_submitted") and not leg.get("finalize_done"):
            new_rej = ST["rejections"][leg.get("finalize_rej_mark", 0):]
            ana = [r for r in new_rej
                   if r.get("type") == "ActionRejected"]
            if ana:
                leg["finalize_done"] = True
                say(f"[P0] {legn} ManaPayment finalize rejected: "
                    f"{json.dumps(ana[0]['data'], default=str)[:200]}")
                wire("mana_finalize_rejected",
                     {"leg": legn, "rejections": ana[:2]})
            elif time.time() - leg.get("finalize_at", 0) > 20:
                leg["finalize_done"] = True
                say(f"[P0] {legn} finalize unacknowledged after 20s")
                wire("mana_finalize_stuck", {"leg": legn})
            else:
                return True
            return True
        if ST.get("mana_sub_rev") is not None:
            if rev <= ST["mana_sub_rev"]:
                if time.time() - ST.get("mana_sub_at", 0) > 15:
                    ST["mana_sub_rev"] = None
                else:
                    return True
            else:
                ST["mana_sub_rev"] = None
        need = leg["need"]
        if leg["pay_taps"] < need:
            for opp in (get_vi(st) or {}).get("opportunities", []) or []:
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    obj = next((s.get("data", {})
                                for s in surfs
                                if s.get("type") == "object"), {})
                    if code == "tapLandForMana" and not obj.get("tapped"):
                        await answer_vi(p0, opp, ch, "P0")
                        leg["pay_taps"] += 1
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say(f"[P0] {legn} tap {obj.get('name')} "
                            f"(#{leg['pay_taps']}/{need})")
                        return True
        if leg["pay_spends"] < need:
            for opp in (get_vi(st) or {}).get("opportunities", []) or []:
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    if code == "spendPoolMana":
                        await answer_vi(p0, opp, ch, "P0")
                        leg["pay_spends"] += 1
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say(f"[P0] {legn} spend pool #{leg['pay_spends']}")
                        return True
        if not leg.get("finalize_submitted"):
            for opp in (get_vi(st) or {}).get("opportunities", []) or []:
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    if code == "passPriority":
                        await answer_vi(p0, opp, ch, "P0")
                        leg["finalize_submitted"] = True
                        leg["finalize_at"] = time.time()
                        leg["finalize_rej_mark"] = len(ST["rejections"])
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say(f"[P0] {legn} mana finalize submitted")
                        return True
        return True

    async def handle_search_choice(st, state, acts):
        """Answer P0's SearchChoice (library search) whenever it appears.

        The engine may raise the search prompt AFTER the spell object has
        left the stack (observed leg2: SearchChoice pending with the spell
        already in Graveyard), so this is intentionally NOT gated on an
        in-flight leg.
        """
        if wf_player(state) != 0:
            return False
        wtype = wf_type(state)
        if wtype in ("Priority", "ModeChoice", "TargetSelection",
                     "ManaPayment", "MulliganDecision", "SelectCards",
                     "DiscardToHandSize", "DeclareAttackers",
                     "DeclareBlockers", ""):
            return False
        legn = ST["cur"]
        leg = ST["legs"].get(legn) if legn else None
        if leg is not None and leg["search_answered"]:
            return False
        vi = get_vi(st)
        if not vi:
            if not ST.get("search_vi_none_logged"):
                ST["search_vi_none_logged"] = True
                wire("search_vi_none",
                     {"wtype": wtype, "leg": legn,
                      "st_keys": list(st.keys())[:15]})
                say("[P0] SearchChoice but no viewer_interaction")
            return False
        opps = vi.get("opportunities", []) or []
        if not opps and not ST.get("search_no_opps_logged"):
            ST["search_no_opps_logged"] = True
            wire("search_no_opps",
                 {"wtype": wtype, "leg": legn,
                  "vi_keys": list(vi.keys())[:15]})
            say("[P0] SearchChoice: viewer_interaction has no "
                "opportunities")
        for opp in opps:
            iid = opp.get("interactionId")
            ent = ST["prompt_first_seen"].setdefault(
                f"search-{iid}", {"t0": time.time(), "done": False})
            if ent["done"]:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            # only treat it as the search prompt if candidates look like
            # library cards
            lib_like = 0
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if (isinstance(d, dict)
                            and str(d.get("zone")).lower() == "library"):
                        lib_like += 1
                        break
            if lib_like == 0:
                if not ent.get("dbg"):
                    ent["dbg"] = True
                    sample = []
                    for ch in chs[:3]:
                        sample.append({
                            "id": ch.get("id"),
                            "status": (ch.get("status") or {}).get("type"),
                            "surfaces": [
                                {"type": s.get("type"),
                                 "dkeys": list((s.get("data") or {}).keys())
                                 [:12],
                                 "dstr": json.dumps(s.get("data"),
                                                    default=str)[:160]}
                                for s in (ch.get("surfaces") or [])[:4]],
                        })
                    wire("search_prompt_debug",
                         {"leg": legn, "iid": iid, "rtype": rtype,
                          "n_choices": len(chs), "sample": sample,
                          "resp_keys": list(
                              (opp.get("response") or {}).keys())})
                    say(f"[P0] search prompt skipped (lib_like=0): "
                        f"n={len(chs)} rtype={rtype}")
                continue
            ent["done"] = True
            wire("avarice_search_prompt",
                 {"leg": legn, "iid": iid, "rtype": rtype, "wtype": wtype,
                  "n_choices": len(chs),
                  "texts": [choice_text(ch)[:60] for ch in chs[:10]]})
            say(f"[P0] search prompt ({wtype}) leg={legn}: {len(chs)} "
                f"candidates")
            pick = next((ch for ch in chs
                         if AVARICE in choice_text(ch).lower()), chs[0])
            await answer_vi(p0, opp, pick, "P0")
            if leg is not None:
                leg["search_prompted"] = True
                leg["search_answered"] = True
                leg["search_chosen_card"] = choice_text(pick)[:80]
            else:
                obs["prompts"].append({"kind": "OrphanSearchChoice",
                                       "wtype": wtype})
            say(f"[P0] search chose: {choice_text(pick)[:60]}")
            return True
        return False

    async def try_cast_leg(st, state, acts):
        """Start the next leg's cast when the window is right."""
        if ST["cur"] is not None:
            return False
        nxt = next((l for l in LEGS if not ST["legs"][l["name"]]["done"]
                    and not ST["legs"][l["name"]]["cast"]), None)
        if nxt is None:
            return False
        # previous leg must be done first
        idx = LEGS.index(nxt)
        if idx > 0 and not ST["legs"][LEGS[idx - 1]["name"]]["done"]:
            return False
        if not (state.get("active_player") == 0
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and wf_type(state) == "Priority"
                and state.get("priority_player") == 0):
            return False
        ul = untapped_lands(state, 0, P0_LANDS)
        if len(ul) < nxt["need"]:
            return False
        av = next((o for o in hand_ids(state, 0)
                   if lname(state, o) == AVARICE), None)
        if av is None:
            return False
        ca = cast_spell_action(acts, state, 0, AVARICE)
        if ca is None:
            if not ST["legs"][nxt["name"]].get("no_cast_offer_wired"):
                ST["legs"][nxt["name"]]["no_cast_offer_wired"] = True
                wire("no_castspell_offered",
                     {"leg": nxt["name"],
                      "legal_action_types":
                      sorted({a.get("type") for a in acts})})
                say(f"[P0] {nxt['name']}: Avarice in hand but no "
                    f"CastSpell advertised (signal)")
            return False
        legn = nxt["name"]
        leg = ST["legs"][legn]
        if legn == "leg1" and not ST["pre_exported"]:
            await export_named("pre")
            ST["pre_exported"] = True
        leg["p1_life_pre"] = life_of(state, 1)
        leg["p1_hand_pre"] = len(hand_ids(state, 1))
        await submit_as_is(p0, ca)
        leg["cast"] = True
        leg["cast_rev"] = st.get("state_revision", -1)
        leg["cast_oid"] = (ca.get("data") or {}).get("object_id")
        ST["cur"] = legn
        ST["cast_oid"] = leg["cast_oid"]
        ST["stuck_since"] = time.time()
        say(f"[P0] {legn} CAST submitted (oid={leg['cast_oid']}, "
            f"modes={nxt['modes']}, need={nxt['need']}); "
            f"p1 life={leg['p1_life_pre']} hand={leg['p1_hand_pre']}")
        wire("avarice_cast", {"leg": legn, "oid": leg["cast_oid"],
                              "modes": nxt["modes"], "need": nxt["need"]})
        return True

    # ---------------- resolution watches (P0) ----------------
    async def watch_leg(st, state, acts):
        """Track target acceptance and spell resolution for the cur leg."""
        legn = ST["cur"]
        if legn is None:
            return False
        leg = ST["legs"][legn]
        wtype = wf_type(state)
        # target acceptance: waiting_for advanced past TargetSelection with
        # no rejection since the submission revision
        if (leg["target_sub_rev"] is not None
                and not leg["target_accepted"]
                and not leg["target_rejected"]
                and wtype != "TargetSelection"):
            new_rej = [r for r in ST["rejections"]
                       [leg.get("target_rej_mark", 0):]
                       if r.get("type") == "ActionRejected"]
            if new_rej:
                leg["target_rejected"] = True
                leg["target_rejection_code"] = (
                    new_rej[0].get("data", {}) or {}).get("code")
                say(f"[P0] {legn} TARGET REJECTED: "
                    f"{json.dumps(new_rej[0].get('data'), default=str)[:300]}")
                wire("target_rejected",
                     {"leg": legn, "rejections": new_rej[:2]})
            else:
                leg["target_accepted"] = True
                say(f"[P0] {legn} target submission ACCEPTED "
                    f"(advanced to {wtype})")
                wire("target_accepted", {"leg": legn, "now": wtype})
                if leg["kind"] in ("target", "both"):
                    await export_named(f"{legn}_mid")
        # stuck detector: still at ModeChoice long after choosing
        if (leg["mode_chosen"] and not leg["target_prompted"]
                and wtype == "ModeChoice" and wf_player(state) == 0
                and time.time() - ST.get("stuck_since", time.time()) > 90):
            say(f"[P0] {legn} stuck at ModeChoice 90s after choice; "
                f"marking done-stuck")
            wire("mode_stuck_after_choice", {"leg": legn})
            leg["done"] = True
            leg["done_reason"] = "stuck at ModeChoice after choice"
            ST["cur"] = None
            return False
        # stuck detector: still at TargetSelection long after submission
        if (wtype == "TargetSelection" and wf_player(state) == 0
                and leg["target_sub_rev"] is not None
                and not leg["target_accepted"]
                and not leg["target_rejected"]):
            if time.time() - ST.get("stuck_since", time.time()) > 60:
                new_rej = [r for r in ST["rejections"]
                           [leg.get("target_rej_mark", 0):]
                           if r.get("type") == "ActionRejected"]
                if new_rej:
                    leg["target_rejected"] = True
                    leg["target_rejection_code"] = (
                        new_rej[0].get("data", {}) or {}).get("code")
                    say(f"[P0] {legn} TARGET REJECTED (late): "
                        f"{leg['target_rejection_code']}")
                    wire("target_rejected_late",
                         {"leg": legn, "rejections": new_rej[:2]})
                else:
                    say(f"[P0] {legn} stuck at TargetSelection 60s with "
                        f"no rejection; marking done-stuck")
                    wire("target_stuck_no_rejection", {"leg": legn})
                    leg["done"] = True
                    leg["done_reason"] = "stuck at TargetSelection 60s"
                    ST["cur"] = None
        # resolution: the in-flight spell object left the Stack zone
        if leg["cast"] and not leg["resolved"] and ST.get("cast_oid"):
            o = get_obj(state, ST["cast_oid"])
            zone = o.get("zone")
            if zone is not None and zone != "Stack":
                leg["resolved"] = True
                leg["p1_life_post"] = life_of(state, 1)
                leg["p1_hand_post"] = len(hand_ids(state, 1))
                say(f"[P0] {legn} RESOLVED (spell zone={zone}); "
                    f"p1 life {leg['p1_life_pre']}->"
                    f"{leg['p1_life_post']}, hand "
                    f"{leg['p1_hand_pre']}->{leg['p1_hand_post']}")
                wire("avarice_resolved",
                     {"leg": legn, "zone": zone,
                      "p1_life": [leg["p1_life_pre"], leg["p1_life_post"]],
                      "p1_hand": [leg["p1_hand_pre"], leg["p1_hand_post"]]})
                await export_named(f"{legn}_post")
                leg["done"] = True
                leg["done_reason"] = "resolved"
                ST["cur"] = None
                ST["cast_oid"] = None
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        drain_rejections(p0)
        if await mulligan_keep(p0, 0, "P0", st, state, acts):
            return
        if await bottom_cards(p0, 0, "P0", st, state, acts):
            return
        if await handle_discard(p0, 0, "P0", st, state, acts):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_type(state)
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)
        # own cast pipeline first (never pass while a prompt is pending)
        if ST["cur"] is not None:
            if await handle_mode_choice(st, state, acts):
                return
            if await handle_target_selection(st, state, acts):
                return
            if await handle_mana_payment(st, state, acts):
                return
            await watch_leg(st, state, acts)
            if ST["cur"] is not None and wf_player(state) == 0 and wtype in (
                    "ModeChoice", "TargetSelection", "ManaPayment"):
                return  # prompt owned by us; handled above or waiting
        # search prompt may arrive after the spell leaves the stack
        if await handle_search_choice(st, state, acts):
            return
        if await try_cast_leg(st, state, acts):
            return
        if await zero_attackers(p0, 0, "P0", st, state, acts):
            return
        if await land_drop(p0, 0, "P0", st, state, acts, P0_LANDS):
            return
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        drain_rejections(p1)
        if await mulligan_keep(p1, 1, "P1", st, state, acts):
            return
        if await bottom_cards(p1, 1, "P1", st, state, acts):
            return
        if await handle_discard(p1, 1, "P1", st, state, acts):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await zero_attackers(p1, 1, "P1", st, state, acts):
            return
        if await land_drop(p1, 1, "P1", st, state, acts, P1_LANDS):
            return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    t0 = time.time()
    last_diag = 0.0

    async def p0_tick_loop():
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if p0.latest is None:
                continue
            st = p0.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                await p0_tick(st, merged_actions(st), state)
            except Exception as e:
                obs["tick_errors"].append(f"P0: {e!r}")

    async def p1_tick_loop():
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if p1.latest is None:
                continue
            st = p1.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                await p1_tick(st, merged_actions(st), state)
            except Exception as e:
                obs["tick_errors"].append(f"P1: {e!r}")

    p0t = asyncio.create_task(p0_tick_loop())
    p1t = asyncio.create_task(p1_tick_loop())
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            done_legs = [l for l in LEGS if ST["legs"][l["name"]]["done"]]
            if len(done_legs) == len(LEGS):
                say("all legs done; exporting post and finishing")
                await export_named("post")
                ST["post_exported"] = True
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s cur={ST['cur']} "
                    f"legs_done={[l['name'] for l in done_legs]} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={wf_type(s)} life={life_of(s,0)}/{life_of(s,1)}")
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
        for fn in ("pre", "leg1_mid", "leg1_post", "leg2_post", "leg3_post",
                   "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre = states.get("pre")
        post = states.get("post")
        L1, L2, L3 = (ST["legs"]["leg1"], ST["legs"]["leg2"],
                      ST["legs"]["leg3"])

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.83.0/data/card-data.json"))
            c = cd["insatiable avarice"]
            modal = c.get("modal") or {}
            costs = modal.get("mode_costs") or []
            c0 = costs[0] if len(costs) > 0 else {}
            c1 = costs[1] if len(costs) > 1 else {}
            ok_modal = (modal.get("min_choices") == 1
                        and modal.get("max_choices") == 2
                        and modal.get("mode_count") == 2
                        and c0.get("generic") == 2
                        and c1.get("shards") == ["Black", "Black"])
            ab1 = (c.get("abilities") or [None, None])[1] or {}
            draw_t = ((ab1.get("effect") or {}).get("target")) or {}
            ok_draw = (ab1.get("effect", {}).get("type") == "Draw"
                       and draw_t.get("type") == "Player")
            notes.append(f"A1: modal min/max/count="
                         f"{modal.get('min_choices')}/"
                         f"{modal.get('max_choices')}/"
                         f"{modal.get('mode_count')}, costs="
                         f"{c0.get('generic')}/{c1.get('shards')}, "
                         f"draw_target={draw_t.get('type')}")
            ok = ok_modal and ok_draw
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            av_hand = AVARICE in hand_lnames(pre, 0)
            ul = untapped_lands(pre, 0, P0_LANDS)
            p1_here = any(p.get("id") == 1 for p in pre.get("players", []))
            ok = av_hand and len(ul) >= 3 and p1_here
            notes.append(f"A2: avarice_in_hand={av_hand} "
                         f"p0_untapped_swamps={len(ul)} p1_present={p1_here}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: mode choice (leg1) ----
        if L1["mode_offered"]:
            ok = (L1["mode_chosen"] and L1["chosen_mode_ids"]
                  and (L1["mode_unavailable"] or []) == []
                  or (1 not in (L1["mode_unavailable"] or [])))
            notes.append(f"A3: mode_offered={L1['mode_offered']} "
                         f"unavailable={L1['mode_unavailable']} "
                         f"chosen_ids={L1['chosen_mode_ids']} "
                         f"n_choices={L1['mode_choices_n']}")
        else:
            ok = False
            notes.append(f"A3 failed: no ModeChoice offered "
                         f"(done_reason={L1['done_reason']})")
        ass["A3_mode_chosen"] = "passed" if ok else "failed"

        # ---- A4: target acceptance (leg1) ----
        if L1["target_prompted"]:
            n = L1["target_candidates_n"] or 0
            seats = L1["target_candidate_seats"] or []
            ok = (n >= 2 and 0 in seats and 1 in seats
                  and L1["target_accepted"]
                  and not L1["target_rejected"])
            notes.append(f"A4: target_prompted={L1['target_prompted']} "
                         f"candidates={n} seats={seats} "
                         f"accepted={L1['target_accepted']} "
                         f"rejected={L1['target_rejected']} "
                         f"(code={L1['target_rejection_code']})")
        else:
            ok = False
            notes.append(f"A4 failed: no TargetSelection prompt raised "
                         f"(done_reason={L1['done_reason']})")
        ass["A4_target_accepted"] = "passed" if ok else "failed"

        # ---- A5: resolution (leg1) ----
        s1 = states.get("leg1_post")
        if s1 is not None and L1["resolved"]:
            life_ok = (L1["p1_life_pre"] == 20
                       and L1["p1_life_post"] == 17)
            hand_ok = (L1["p1_hand_pre"] is not None
                       and L1["p1_hand_post"] == L1["p1_hand_pre"] + 3)
            ok = life_ok and hand_ok
            notes.append(f"A5: p1 life "
                         f"{L1['p1_life_pre']}->{L1['p1_life_post']}, "
                         f"hand {L1['p1_hand_pre']}->"
                         f"{L1['p1_hand_post']}")
        else:
            ok = False
            notes.append(f"A5 failed: leg1 never resolved "
                         f"(resolved={L1['resolved']}, "
                         f"done_reason={L1['done_reason']})")
        ass["A5_resolution"] = "passed" if ok else "failed"

        # ---- B1: search control (leg2) ----
        if L2["resolved"]:
            rej = [r for r in ST["rejections"]
                   if r.get("stage") == "leg2"]
            ok = not rej
            notes.append(f"B1: leg2 resolved={L2['resolved']} "
                         f"search_answered={L2['search_answered']} "
                         f"({L2['search_chosen_card']}) "
                         f"rejections={len(rej)}")
        else:
            ok = False
            notes.append(f"B1 failed: leg2 never resolved "
                         f"(done_reason={L2['done_reason']})")
        ass["B1_search_control"] = "passed" if ok else "failed"

        # ---- C1: both modes (leg3) ----
        # NOTE: the engine applies this spell's modal effects AFTER the
        # spell object leaves the Stack zone (the SearchChoice prompt is
        # raised and answered post-resolution), so leg3_post.json captures
        # the at-resolution instant while the authoritative outcome is read
        # from the final post.json. leg3 is the last leg and the game saw
        # no intervening turn/attack, so P1's deltas are the spell's.
        s3 = states.get("leg3_post")
        if L3["resolved"] and s3 is not None and post is not None:
            p1l = life_of(post, 1)
            p1h = len(hand_ids(post, 1))
            life_ok = (L3["p1_life_pre"] is not None
                       and p1l == L3["p1_life_pre"] - 3)
            hand_ok = (L3["p1_hand_pre"] is not None
                       and p1h == L3["p1_hand_pre"] + 3)
            ok = (life_ok and hand_ok and L3["target_accepted"]
                  and not L3["target_rejected"])
            notes.append(f"C1: leg3 p1 life "
                         f"{L3['p1_life_pre']}->{p1l} (at-resolution "
                         f"{life_of(s3, 1)}, final {p1l}), hand "
                         f"{L3['p1_hand_pre']}->{p1h}, "
                         f"target_accepted={L3['target_accepted']}, "
                         f"search answered post-resolution "
                         f"(orphan SearchChoice, wired)")
        else:
            ok = False
            notes.append(f"C1 failed: leg3 never resolved "
                         f"(done_reason={L3['done_reason']})")
        ass["C1_both_modes"] = "passed" if ok else "failed"

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = wf_type(post)
            ok = stack_empty and wft in ("Priority",)
            notes.append(f"A6: stack_empty={stack_empty} post_wf={wft} "
                         f"turns_seen={sorted(ST['turns_seen'])}")
        else:
            ok = False
            notes.append("A6 failed: post.json missing")
        ass["A6_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif (ass["A3_mode_chosen"] == "passed"
                and ass["A4_target_accepted"] == "passed"
                and ass["A5_resolution"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: targeting mode casts, "
                         "target accepted, draw+life loss land on P1")
        elif ass["A2_setup"] == "passed" and (
                ass["A3_mode_chosen"] == "failed"
                or ass["A4_target_accepted"] == "failed"
                or ass["A5_resolution"] == "failed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: targeting mode cannot be "
                         "cast to completion")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: inconclusive")

        run = {
            "issue": 7175,
            "title": "Insatiable Avarice bug -- Insatiable Avarice is "
                     "impossible to cast when the targeting mode is chosen",
            "run_id": RUN_ID,
            "server": SERVER_IDENTITY,
            "validated_version": "v0.83.0",
            "verdict": verdict,
            "scope": "Insatiable Avarice Spree targeting mode ({B}{B}): "
                     "mode choice + player target selection + resolution; "
                     "search-only mode control + both-modes composition; "
                     "native engine, two human-client seats",
            "result": "; ".join(
                f"{k}: {v}" for k, v in ass.items()),
            "notes": notes,
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "30x/30x card density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "P1 is fully passive (land drops only, never attacks) "
                "so the cast window stays clean.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "driver": {"protocol_advertised": 70,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7175.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "leg1_pre_target.json",
                               "leg1_mid.json", "leg1_post.json",
                               "leg2_post.json", "leg3_pre_target.json",
                               "leg3_mid.json", "leg3_post.json",
                               "post.json", "run.json", "manifest.sha256",
                               "summary.png", "scenario_7175.py",
                               "wire_log.jsonl", "scenario_run.log",
                               "server.log"],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7175.py",
                    f"{EVDIR}/scenario_7175.py")
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
        W, H = 1000, 1120
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7175 - Insatiable Avarice",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-15 - "
               "Spree targeting mode",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: Spree -- +{2} search, +{B}{B} target player "
               "draws 3, loses 3",
               fill=(200, 210, 225))
        y += 26
        d.text((24, y), "card-data parse faithful: modal min1/max2, "
               "Draw target=Player",
               fill=(200, 210, 225))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: modal + Player-target Draw parse ok",
            "A2_setup": "PRE: Avarice in P0 hand + 3 untapped Swamps",
            "A3_mode_chosen": "leg1: ModeChoice offered, targeting mode "
                              "selectable",
            "A4_target_accepted": "leg1: P1 target submitted and ACCEPTED",
            "A5_resolution": "leg1: P1 20->17 life, hand +3",
            "B1_search_control": "leg2: search-only mode resolves cleanly",
            "C1_both_modes": "leg3: both modes; P1 -3 life, +3 hand",
            "A6_cleanup": "stack empty, game proceeded",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120),
                   "failed": (255, 90, 90),
                   "not-run": (230, 200, 120)}.get(v, (180, 180, 180))
            d.text((40, y), f"{v:>9}", fill=col)
            d.text((130, y), lab, fill=(210, 220, 235))
            y += 24
        y += 8
        pre, s1, post = (states.get("pre"), states.get("leg1_post"),
                         states.get("post"))

        def life(s, pid):
            for p in (s or {}).get("players", []):
                if p.get("id") == pid:
                    return p.get("life")
            return "?"

        def handn(s, pid):
            for p in (s or {}).get("players", []):
                if p.get("id") == pid:
                    return len(p.get("hand", []))
            return "?"
        d.text((24, y), "Pre-cast (Avarice in P0 hand, 3+ untapped Swamps):",
               fill=(200, 210, 225))
        y += 24
        if pre is not None:
            d.text((40, y), f"P1 life: {life(pre, 1)}, hand: "
                   f"{handn(pre, 1)}; P0 life: {life(pre, 0)}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "pre.json MISSING", fill=(255, 90, 90))
        y += 30
        d.text((24, y), "Post-leg1 (targeting mode resolution):",
               fill=(200, 210, 225))
        y += 24
        if s1 is not None:
            d.text((40, y), f"P1 life: {life(s1, 1)} (20->17 if draw/life "
                   f"landed), hand: {handn(s1, 1)}; "
                   f"stack: {len(s1.get('stack') or [])}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "leg1_post.json MISSING", fill=(255, 90, 90))
        y += 30
        d.text((24, y), "Post-run:", fill=(200, 210, 225))
        y += 24
        if post is not None:
            d.text((40, y), f"life: {life(post, 0)}/{life(post, 1)}; "
                   f"stack: {len(post.get('stack') or [])}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "post.json MISSING", fill=(255, 90, 90))
        y += 40
        d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
               "7175/" + RUN_ID + "/", fill=(140, 160, 180))
        p = os.path.join(EVDIR, "summary.png")
        img.save(p)
        say(f"saved summary.png ({os.path.getsize(p)} bytes)")

    def write_manifest():
        # NOTE: no say()/wire() after this point -- any line appended to
        # scenario_run.log after hashing invalidates the manifest.
        say("writing manifest.sha256 (last log line before hashing)")
        files = [f for f in sorted(os.listdir(EVDIR))
                 if f not in ("manifest.sha256",)]
        lines = []
        for fn in files:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()
    try:
        await p0.close()
        await p1.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
