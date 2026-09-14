#!/usr/bin/env python3
"""Issue #7142: Nykthos Paragon - "offers the ability to give counters for
lifegain only once."

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0, key "nykthos paragon"):
  Nykthos Paragon ({4}{W}{W}, 4/6 Enchantment Creature - Human Soldier):
    "Whenever you gain life, you may put that many +1/+1 counters on each
     creature you control. Do this only once each turn."

Card-data parse state on v0.82.0 (verified 2026-09-13 before the run):
  triggers[0] = LifeGained(valid_target=Controller) ->
    PutCounter P1P1 count=Ref(EventContextAmount)
    target=Typed(Creature, controller=You), optional=true,
    constraint=OncePerTurn, batched=false.
  Parse-shape observations (not asserted): the "each creature" application is
  encoded as a Typed target (no explicit all-matching marker), and batched is
  false.

Reported symptom (Discord, triage-clarified by mike-theDude):
  1. Nykthos Paragon offers the counter placement only once: declining the
     offer for one lifegain instance suppresses the offer for later lifegain
     instances (same turn).
  2. Accepting puts the counters on a single creature instead of each
     creature controlled.

Expected behavior (triage acceptance criteria):
  - Declining preserves a later eligible trigger that turn.
  - Accepting marks the action used only after counters are placed.
  - Every currently controlled creature receives the correct amount.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Nykthos Paragon, 12x Grizzly Bears, 12x Healing Salve,
      18x Plains, 6x Forest.
  P1: 60x Forest (passive; never plays lands/casts/attacks/blocks).

Planned line:
  Turns 1+: P0 drops lands (Plains-first), casts 2+ Grizzly Bears, casts
    Nykthos Paragon ({4}{W}{W}). Neither player attacks.
  Leg 1 (decline leg): with Paragon + 2 bears on the battlefield, 2+ Healing
    Salves in hand, and 2+ untapped Plains, export pre.json, then cast
    Healing Salve #1 targeting self (mode: gain 3 life). P0 20->23.
    The Nykthos optional prompt is EXPECTED; the driver DECLINES it.
    offer1.json is exported at the offer.
  Leg 2 (accept leg): cast Healing Salve #2 targeting self (mode: gain 3).
    P0 23->26. The Nykthos optional prompt is EXPECTED again (declining leg 1
    must not consume the once-per-turn opportunity); the driver ACCEPTS it.
    offer2.json is exported at the offer. post.json is exported once the
    trigger resolves and the game settles.
  BACKFILL_7142_MODE=accept: single-leg run. Same setup, one Salve; ACCEPT
    the offer and check EVERY creature gains 3 counters. A4 is n/a.
    NOTE: on v0.82.0 the accept path raises a ChooseFromZoneChoice asking
    P0 to pick exactly ONE creature for the counters (the each-creature
    bug mechanism); the driver answers it (first bear) and asserts the
    per-creature deltas.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: LifeGained -> PutCounter[P1P1 x
                      EventContextAmount] on Typed(Creature, You),
                      optional, OncePerTurn.
  A2_setup            PRE: Paragon on P0 BF, 2+ bears on P0 BF, 2+ Salves in
                      P0 hand, P0 at 20 life.
  A3_offer1           lifegain #1 raised the Nykthos optional prompt for P0.
  A4_decline_preserves
                      after declining leg 1, lifegain #2 raised the Nykthos
                      optional prompt again (reported bug: it does not).
  A5_counters_all     accept leg: every P0-controlled creature gained exactly
                      3 +1/+1 counters vs pre.json (reported bug: only one
                      creature does).
  A6_cleanup          both Salves in P0 graveyard, P0 at 26, stack empty,
                      game proceeds.

Verdict rule:
  blocked        iff A2 fails (setup never reached) or a leg never executed.
  reproduced     iff A2 passes and any executed leg assertion (A3/A4/A5)
                 fails.
  not-reproduced iff A2..A6 all pass.

Evidence: evidence/7142/<run-id>/pre.json, offer1.json, offer2.json,
post.json, run.json, manifest.sha256, summary.png, scenario_7142.py,
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7142")
EVDIR = f"{BACKFILL}/evidence/7142/{RUN_ID}"
# MODE=decline: two-salve run, decline leg 1, check the offer returns (A4),
#   then accept leg 2 and check each-creature counters (A5).
# MODE=accept: single-salve run, accept the first offer and check
#   each-creature counters (A5) directly. Used because the decline bug
#   (A4) makes the accept leg unreachable in decline mode.
MODE = os.environ.get("BACKFILL_7142_MODE", "decline")
assert MODE in ("decline", "accept"), f"bad BACKFILL_7142_MODE={MODE}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

NYKTHOS = "nykthos paragon"
BEAR = "grizzly bears"
SALVE = "healing salve"
PLAINS = "plains"
FOREST = "forest"
LANDS = (PLAINS, FOREST)

P0_DECK = [(NYKTHOS, 12), (BEAR, 12), (SALVE, 12), (PLAINS, 18), (FOREST, 6)]
P1_DECK = [(FOREST, 60)]

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


def untapped_plains(state, pid):
    return [oid for oid in untapped_lands(state, pid)
            if lname(state, oid) == PLAINS]


def exile_oids(state):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if str(o.get("zone", "")).lower() == "exile"]


def gy_oids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def counters_of(state, oid):
    """Best-effort +1/+1 counter count on an object, across engine shapes."""
    o = get_obj(state, oid)
    total = 0
    found = False
    c = o.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            if isinstance(v, (int, float)) and "P1P1" in str(k).upper().replace(
                    "+", "P").replace(" ", "") or "1/1" in str(k) or \
                    "p1p1" in str(k).lower():
                total += int(v)
                found = True
            elif isinstance(v, (int, float)):
                # any other counter kind recorded separately; only +1/+1 here
                pass
    elif isinstance(c, list):
        for e in c:
            if isinstance(e, dict):
                k = str(e.get("type") or e.get("kind")
                        or e.get("counter_type") or "")
                v = e.get("count", e.get("n", 1))
                if "P1P1" in k.upper().replace("+", "P") or \
                        "1/1" in k or "p1p1" in k.lower():
                    try:
                        total += int(v)
                        found = True
                    except Exception:
                        pass
            elif isinstance(e, str) and "1/1" in e:
                total += 1
                found = True
    for k in ("plus_one_plus_one_counters", "p1p1_counters",
              "plusOnePlusOneCounters", "p1p1Counters"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
            found = True
    return total, found


def pt_of(state, oid):
    o = get_obj(state, oid)
    return o.get("power"), o.get("toughness")


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
        "stage": "setup",          # setup -> salve1 -> salve2 -> done
        "stage_t0": time.time(),
        "pre_exported": False, "offer1_exported": False,
        "offer2_exported": False, "post_exported": False,
        "prompt_first_seen": {},
        "land_turn": -1,
        "paragon_cast": False, "paragon_cast_turn": None,
        "bears_cast": 0,
        # leg 1 (decline)
        "salve1_cast": False, "salve1_oid": None,
        "salve1_mode_chosen": False,
        "salve1_target_oid": None,   # actually submitted target candidate
        "salve1_resolved": False, "salve1_resolve_t": None,
        "offer1_seen": False, "offer1_skipped": False,
        "offer1_kind": None, "offer1_iid": None,
        "offer1_declined": False, "offer1_accepted": False,
        "choice_seen": False, "choice_exported": False,
        "choice_answered": False, "choice_oid": None,
        "choice_count": None, "choice_cards": [], "choice_source": None,
        "p0_life_pre": None, "p0_life_post_salve1": None,
        # leg 2 (accept)
        "salve2_cast": False, "salve2_oid": None,
        "salve2_mode_chosen": False,
        "salve2_target_oid": None,
        "salve2_resolved": False, "salve2_resolve_t": None,
        "offer2_seen": False, "offer2_skipped": False,
        "offer2_kind": None, "offer2_iid": None,
        "offer2_accepted": False,
        "p0_life_post_salve2": None,
        "settle_ticks": 0,
        "game_code": None,
        "last_rev_acted": {},
        "zones_seen": set(),
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "prompts": [], "stage_timeouts": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-nykthos")
    p1 = PhaseClient("P1-passive")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    say(f"BACKFILL_7142_MODE={MODE}")
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
            if not ent.get("shape_wired"):
                ent["shape_wired"] = True
                wire("discard_opportunity_shape",
                     {"who": tag, "iid": iid,
                      "wf_type": wf_of(state).get("type"),
                      "n_choices": len(chs),
                      "choice_ids": [ch.get("id") for ch in chs][:8],
                      "first_choice":
                      json.loads(json.dumps(chs[0], default=str)),
                      "ref_probe": [ref_of(ch) for ch in chs][:8],
                      "texts": [choice_text(ch)[:40] for ch in chs][:8]})

            def rank(ch):
                nm = str(choice_text(ch)).lower()
                if nm in LANDS:
                    return 0
                if nm == BEAR:
                    return 1
                if nm == SALVE:
                    return 2
                return 3  # paragon: protect
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: "
                f"{choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    async def salve_mode_choice(c, pid, tag, st, state, acts):
        """Healing Salve is modal ('Choose one'). The engine raises a
        ModeChoice waiting_for whose data.modal.mode_descriptions lists the
        modes in order; choices carry no text surfaces (like discards), so
        the lifegain mode is picked by its index in mode_descriptions.
        Fires only while a Salve is cast but not yet resolved."""
        wtype = wf_of(state).get("type") or ""
        if not wf_player_is(state, pid):
            return False
        leg = None
        if ST["stage"] == "salve1" and ST["salve1_cast"] \
                and not ST["salve1_resolved"] and not ST["salve1_mode_chosen"]:
            leg = 1
        elif ST["stage"] == "salve2" and ST["salve2_cast"] \
                and not ST["salve2_resolved"] and not ST["salve2_mode_chosen"]:
            leg = 2
        if leg is None:
            return False
        wf_data = wf_of(state).get("data") or {}
        modal = wf_data.get("modal") or {}
        if wtype != "ModeChoice" or not modal:
            ent = ST["prompt_first_seen"].setdefault(
                f"nonmode-{wtype}-{ST['stage']}",
                {"t0": time.time(), "done": False})
            if not ent.get("wired"):
                ent["wired"] = True
                wire("unexpected_pre_mode_prompt",
                     {"who": tag, "leg": leg, "wf_type": wtype,
                      "wf_data_keys": list(wf_data.keys()),
                      "legal_action_types":
                      sorted({a.get("type") for a in acts}),
                      "vi": json.loads(json.dumps(get_vi(st), default=str))})
                obs["unexpected_prompts"].append(
                    {"leg": leg, "wf_type": wtype})
                say(f"[{tag}] leg {leg}: expected ModeChoice, saw {wtype}; "
                    "wired for diagnosis")
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
            modes = modal.get("mode_descriptions") or []
            wire("salve_mode_choice",
                 {"who": tag, "leg": leg, "iid": iid, "wf_type": wtype,
                  "mode_descriptions": modes,
                  "n_choices": len(chs),
                  "choice_ids": [ch.get("id") for ch in chs],
                  "texts": [choice_text(ch)[:60] for ch in chs],
                  "accept_values": [accept_of(ch) for ch in chs],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": "SalveModeChoice", "who": tag,
                                   "leg": leg, "wf_type": wtype,
                                   "modes": modes})
            life_idx = next(
                (i for i, m in enumerate(modes)
                 if "gains 3 life" in str(m).lower()), 0)
            # prefer a text match when surfaces carry text; otherwise the
            # mode_descriptions order (lifegain is modes[life_idx]).
            pick = next((ch for ch in chs
                         if "gains 3 life" in choice_text(ch).lower()), None)
            if pick is None:
                pick = chs[life_idx] if life_idx < len(chs) else chs[0]
            say(f"[{tag}] salve mode choice (leg {leg}): modes={modes} "
                f"picking index {chs.index(pick)} "
                f"({choice_text(pick)[:50]})")
            await answer_vi(c, opp, pick, tag)
            if leg == 1:
                ST["salve1_mode_chosen"] = True
            else:
                ST["salve2_mode_chosen"] = True
            ent["done"] = True
            return True
        return False

    def is_nykthos_offer_shape(wtype, chs):
        """Distinguish the Paragon optional-trigger prompt from the Salve
        mode choice by shape: accept true/false values, 'may'/'counter'
        text, or an Optional/Trigger waiting_for type."""
        if "ptional" in wtype or "Trigger" in wtype:
            return True
        avals = {accept_of(ch) for ch in chs}
        if avals & {"true", "false"}:
            return True
        joined = " ".join(choice_text(ch).lower() for ch in chs)
        if any(k in joined for k in ("+1/+1 counter", "counters on each",
                                     "you may put")):
            return True
        if len(chs) == 2 and all(
                choice_text(ch).strip().lower() in
                ("yes", "no", "y", "n", "accept", "decline",
                 "pay", "don't pay") for ch in chs):
            return True
        return False

    async def nykthos_offer(c, pid, tag, st, state):
        """Handle the Paragon's optional-trigger prompt. Fires only after the
        leg's Salve resolved (life went up): decline in leg 1, accept in
        leg 2. Records the actually-answered prompt for assertions."""
        wtype = wf_of(state).get("type") or ""
        if ST["stage"] not in ("salve1", "salve2"):
            return False
        if not wf_player_is(state, pid):
            return False
        leg = 1 if ST["stage"] == "salve1" else 2
        resolved = ST["salve1_resolved"] if leg == 1 else ST["salve2_resolved"]
        if leg == 1:
            done = ST["offer1_declined"] or ST["offer1_accepted"]
        else:
            done = ST["offer2_accepted"]
        if not resolved or done:
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
            if not is_nykthos_offer_shape(wtype, chs):
                # wire unclassified opportunities in the offer window once,
                # so a differently-shaped offer is still on the record
                if not ent.get("unclass_wired"):
                    ent["unclass_wired"] = True
                    wire("unclassified_offer_window_opportunity",
                         {"who": tag, "leg": leg, "iid": iid,
                          "wf_type": wtype,
                          "wf_data_keys": list(
                              (wf_of(state).get("data") or {}).keys()),
                          "n_choices": len(chs),
                          "choice_ids": [ch.get("id") for ch in chs],
                          "texts": [choice_text(ch)[:60] for ch in chs],
                          "accept_values": [accept_of(ch) for ch in chs],
                          "action_codes":
                          [action_codes(ch) for ch in chs]})
                    say(f"[{tag}] leg {leg}: unclassified opportunity in "
                        f"offer window (wf={wtype}); wired")
                continue
            # snapshot creature objects at the offer for the wire log
            st_now = st.get("state") or {}
            wire("nykthos_offer",
                 {"who": tag, "leg": leg, "iid": iid, "wf_type": wtype,
                  "wf_data": wf_of(state).get("data"),
                  "n_choices": len(chs),
                  "accept_values": [accept_of(ch) for ch in chs],
                  "action_codes": [action_codes(ch) for ch in chs],
                  "texts": [choice_text(ch)[:80] for ch in chs],
                  "creatures": {
                      str(oid): get_obj(st_now, oid)
                      for oid in bf_ids(st_now, 0)
                      if "creature" in str(
                          get_obj(st_now, oid).get("card_type") or "")
                      .lower() or lname(st_now, oid) in (NYKTHOS, BEAR)},
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            obs["prompts"].append({"kind": wtype or "NykthosOffer",
                                   "who": tag, "leg": leg,
                                   "accept_values":
                                   [accept_of(ch) for ch in chs]})
            say(f"[{tag}] NYKTHOS offer (leg {leg}, wf={wtype}): "
                f"n={len(chs)} accept={[accept_of(ch) for ch in chs]} "
                f"texts={[choice_text(ch)[:50] for ch in chs]}")
            if leg == 1:
                if not ST["offer1_exported"]:
                    if await export_named("offer1"):
                        ST["offer1_exported"] = True
                ST["offer1_seen"] = True
                ST["offer1_kind"] = wtype
                ST["offer1_iid"] = iid
                if MODE == "accept":
                    pick = next((ch for ch in chs
                                 if accept_of(ch) == "true"), None)
                    if pick is None:
                        pick = next((ch for ch in chs
                                     if choice_text(ch).strip().lower()
                                     in ("yes", "y", "accept")), None)
                    if pick is None:
                        pick = chs[0]
                    say(f"[P0] ACCEPTING the Paragon counters "
                        f"(accept-only mode)")
                    await answer_vi(c, opp, pick, tag)
                    ST["offer1_accepted"] = True
                else:
                    pick = next((ch for ch in chs
                                 if accept_of(ch) == "false"), None)
                    if pick is None:
                        pick = next((ch for ch in chs
                                     if choice_text(ch).strip().lower()
                                     in ("no", "n", "decline")), None)
                    if pick is None:
                        pick = chs[-1]
                    say(f"[P0] DECLINING the Paragon counters (leg 1)")
                    await answer_vi(c, opp, pick, tag)
                    ST["offer1_declined"] = True
                ent["done"] = True
                return True
            else:
                if not ST["offer2_exported"]:
                    if await export_named("offer2"):
                        ST["offer2_exported"] = True
                ST["offer2_seen"] = True
                ST["offer2_kind"] = wtype
                ST["offer2_iid"] = iid
                pick = next((ch for ch in chs
                             if accept_of(ch) == "true"), None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if choice_text(ch).strip().lower()
                                 in ("yes", "y", "accept")), None)
                if pick is None:
                    pick = chs[0]
                say(f"[P0] ACCEPTING the Paragon counters (leg 2)")
                await answer_vi(c, opp, pick, tag)
                ST["offer2_accepted"] = True
                ent["done"] = True
                return True
        return False

    async def nykthos_choice(c, pid, tag, st, state):
        """After ACCEPTING the offer, the engine (bug) presents a
        ChooseFromZoneChoice asking P0 to pick exactly ONE creature to
        receive the counters, instead of applying to each creature. The
        driver answers by choosing the first listed creature and records
        the prompt as choice.json evidence."""
        if MODE != "accept":
            return False
        if ST["stage"] != "salve1" or not ST["offer1_accepted"]:
            return False
        wf = wf_of(state)
        if (wf.get("type") or "") != "ChooseFromZoneChoice":
            return False
        if not wf_player_is(state, pid):
            return False
        data = wf.get("data") or {}
        cards = data.get("cards") or []
        count = data.get("count")
        src = data.get("source_id")
        if not ST["choice_exported"]:
            if await export_named("choice"):
                ST["choice_exported"] = True
        ST["choice_seen"] = True
        ST["choice_count"] = count
        ST["choice_cards"] = list(cards)
        ST["choice_source"] = src
        names = [lname(state, o) for o in cards]
        say(f"[P0] NYKTHOS choice prompt: choose {count} of "
            f"{len(cards)} creatures {list(zip(cards, names))} "
            f"(source oid={src}) -- the 'each creature' bug mechanism")
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
            # pick the first creature candidate (a bear if listed)
            pick = None
            for ch in chs:
                oid = ref_of(ch)
                if oid in cards and lname(state, oid) == BEAR:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if ref_of(ch) in cards:
                        pick = ch
                        break
            if pick is None:
                pick = chs[0]
            chosen = ref_of(pick)
            wire("nykthos_choice",
                 {"who": tag, "iid": iid, "count": count,
                  "cards": list(cards), "names": names,
                  "source_id": src, "chosen_oid": chosen,
                  "chosen_name": lname(state, chosen)})
            obs["prompts"].append({"kind": "ChooseFromZoneChoice",
                                   "who": tag, "count": count,
                                   "n_cards": len(cards),
                                   "chosen_oid": chosen})
            say(f"[P0] choosing creature {chosen} "
                f"({lname(state, chosen)}) for the counters")
            await answer_vi(c, opp, pick, tag)
            ST["choice_answered"] = True
            ST["choice_oid"] = chosen
            ent["done"] = True
            return True
        return False

    async def target_selection(c, pid, tag, st, state):
        """Healing Salve targets a player: choose self (seat 0). Only the
        Salve targets anything in this scenario; tag the stage."""
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        if not wf_player_is(state, pid):
            return False
        if ST["stage"] not in ("salve1", "salve2"):
            return False
        leg = 1 if ST["stage"] == "salve1" else 2
        in_flight = (ST["salve1_cast"] and not ST["salve1_resolved"]) \
            if leg == 1 else (ST["salve2_cast"] and not ST["salve2_resolved"])
        if not in_flight:
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
            pick = next((ch for ch in chs if seat_of(ch) == 0), None)
            if pick is None:
                pick = chs[0]
            wire("target_selection",
                 {"who": tag, "purpose": f"salve_leg{leg}", "iid": iid,
                  "n": len(chs), "picked_seat": seat_of(pick),
                  "picked_ref": ref_of(pick),
                  "picked_oid": ref_of(pick)})
            obs["prompts"].append({"kind": "TargetSelection", "who": tag,
                                   "purpose": f"salve_leg{leg}",
                                   "picked_seat": seat_of(pick)})
            say(f"[{tag}] TargetSelection (salve leg {leg}): choosing self")
            await answer_vi(c, opp, pick, tag)
            # record the ACTUALLY submitted candidate oid (#6906 lesson)
            if leg == 1:
                ST["salve1_target_oid"] = ref_of(pick)
            else:
                ST["salve2_target_oid"] = ref_of(pick)
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
        if await salve_mode_choice(p0, 0, "P0", st, state, acts):
            return
        if await nykthos_offer(p0, 0, "P0", st, state):
            return
        if await nykthos_choice(p0, 0, "P0", st, state):
            return
        if await target_selection(p0, 0, "P0", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        paragon_bf = bf_ids(state, 0, NYKTHOS)
        bears_bf = bf_ids(state, 0, BEAR)

        # --- combat declarations: never attack, never block ---
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

        # --- main-phase actions ---
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop: Plains-first (need WW + W + W); one Forest for bears
            if ST["land_turn"] != turn:
                have_plains = sum(1 for o in bf_lands(state, 0)
                                  if lname(state, o) == PLAINS)
                have_forest = sum(1 for o in bf_lands(state, 0)
                                  if lname(state, o) == FOREST)
                want = PLAINS if (have_plains < 4 or have_forest >= 1) \
                    else FOREST
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
            if ST["stage"] == "setup":
                # cast bears (2+ on the battlefield; a 3rd only pre-paragon
                # to keep mana open for the 4WW cast)
                if len(bears_bf) < 2 or (len(bears_bf) < 3
                                        and not ST["paragon_cast"]):
                    ca = cast_spell_action(acts, state, 0, BEAR)
                    if ca and not acted("bear", rev):
                        say("[P0] casting grizzly bears")
                        await submit_as_is(p0, ca)
                        ST["bears_cast"] += 1
                        return
                # cast paragon
                if not paragon_bf and not ST["paragon_cast"]:
                    ca = cast_spell_action(acts, state, 0, NYKTHOS)
                    if ca and not acted("paragon", rev):
                        say("[P0] casting nykthos paragon")
                        await submit_as_is(p0, ca)
                        ST["paragon_cast"] = True
                        ST["paragon_cast_turn"] = turn
                        return
                # pre-export gate: paragon + 2 bears + salve(s) + W open
                # (accept mode needs a single salve / single W)
                need_salves = 1 if MODE == "accept" else 2
                need_plains = 1 if MODE == "accept" else 2
                salves_hand = sum(1 for n in hand_lnames(state, 0)
                                  if n == SALVE)
                if (len(paragon_bf) >= 1 and len(bears_bf) >= 2
                        and salves_hand >= need_salves
                        and len(untapped_plains(state, 0)) >= need_plains
                        and not ST["pre_exported"]):
                    ST["p0_life_pre"] = life_of(state, 0)
                    if await export_named("pre"):
                        ST["pre_exported"] = True
                        say(f"[P0] PRE exported: paragon_bf="
                            f"{len(paragon_bf)} bears_bf={len(bears_bf)} "
                            f"salves_hand={salves_hand} life="
                            f"{ST['p0_life_pre']}")
                        set_stage("salve1")
                        return
            elif ST["stage"] == "salve1" and not ST["salve1_cast"]:
                ca = cast_spell_action(acts, state, 0, SALVE)
                if ca and not acted("salve1", rev):
                    oid = (ca.get("data") or {}).get("object_id")
                    say(f"[P0] casting healing salve #1 (oid={oid})")
                    await submit_as_is(p0, ca)
                    ST["salve1_cast"] = True
                    ST["salve1_oid"] = oid
                    return
            elif ST["stage"] == "salve2" and not ST["salve2_cast"]:
                ca = cast_spell_action(acts, state, 0, SALVE)
                if ca and not acted("salve2", rev):
                    oid = (ca.get("data") or {}).get("object_id")
                    say(f"[P0] casting healing salve #2 (oid={oid})")
                    await submit_as_is(p0, ca)
                    ST["salve2_cast"] = True
                    ST["salve2_oid"] = oid
                    return

        # --- leg progress / resolution detection ---
        life0 = life_of(state, 0)
        base = ST["p0_life_pre"]
        if ST["stage"] == "salve1" and ST["salve1_cast"] \
                and not ST["salve1_resolved"] \
                and base is not None and life0 is not None \
                and life0 >= base + 3:
            ST["salve1_resolved"] = True
            ST["salve1_resolve_t"] = time.time()
            ST["p0_life_post_salve1"] = life0
            say(f"[P0] salve #1 resolved: P0 {base}->{life0}")
        if ST["stage"] == "salve2" and ST["salve2_cast"] \
                and not ST["salve2_resolved"] \
                and base is not None and life0 is not None \
                and life0 >= base + 6:
            ST["salve2_resolved"] = True
            ST["salve2_resolve_t"] = time.time()
            ST["p0_life_post_salve2"] = life0
            say(f"[P0] salve #2 resolved: P0 {base}->{life0}")

        # leg 1 settled after the offer was answered
        if ST["stage"] == "salve1" and (
                ST["offer1_declined"] or ST["offer1_accepted"]):
            # in accept mode, don't call it settled while the (buggy)
            # single-creature choice prompt is still pending an answer
            choice_pending = (MODE == "accept" and ST["choice_seen"]
                              and not ST["choice_answered"])
            settled = (not (state.get("stack") or [])) \
                and wtype == "Priority" and not choice_pending
            if settled:
                ST["settle_ticks"] += 1
            else:
                ST["settle_ticks"] = 0
            if ST["settle_ticks"] >= 3:
                if MODE == "accept":
                    say("[P0] accept leg settled; exporting post")
                    if not ST["post_exported"]:
                        if await export_named("post"):
                            ST["post_exported"] = True
                            set_stage("done")
                else:
                    say("[P0] leg 1 settled after decline; starting leg 2")
                    set_stage("salve2")
        # leg 1: offer never came within 75s of resolution -> skipped path
        if ST["stage"] == "salve1" and ST["salve1_resolved"] \
                and not ST["offer1_seen"] and ST["salve1_resolve_t"] \
                and time.time() - ST["salve1_resolve_t"] > 75:
            say("[P0] leg 1: 75s after lifegain with no Nykthos offer; "
                "treating as skipped-offer path")
            ST["offer1_skipped"] = True
            if MODE == "accept":
                if not ST["post_exported"]:
                    if await export_named("post"):
                        ST["post_exported"] = True
                        set_stage("done")
            else:
                set_stage("salve2")
        # leg 2: accept leg settled -> export post -> done
        if ST["stage"] == "salve2" and ST["offer2_accepted"]:
            settled = (not (state.get("stack") or [])) \
                and wtype == "Priority"
            if settled:
                ST["settle_ticks"] += 1
            else:
                ST["settle_ticks"] = 0
            if ST["settle_ticks"] >= 3:
                if not ST["post_exported"]:
                    if await export_named("post"):
                        ST["post_exported"] = True
                        set_stage("done")
        # leg 2: offer never came within 75s of resolution -> skipped path
        if ST["stage"] == "salve2" and ST["salve2_resolved"] \
                and not ST["offer2_seen"] and ST["salve2_resolve_t"] \
                and time.time() - ST["salve2_resolve_t"] > 75:
            say("[P0] leg 2: 75s after lifegain with no Nykthos offer; "
                "treating as skipped-offer path")
            ST["offer2_skipped"] = True
            if not ST["post_exported"]:
                if await export_named("post"):
                    ST["post_exported"] = True
                    set_stage("done")
        # stage safety timeout
        if ST["stage"] in ("salve1", "salve2") \
                and time.time() - ST["stage_t0"] > 420:
            obs["stage_timeouts"].append(
                {"stage": ST["stage"],
                 "t": round(time.time() - ST["stage_t0"], 1)})
            say(f"[P0] stage {ST['stage']} timed out (420s); exporting "
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
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 90 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"paragon_bf={len(bf_ids(s, 0, NYKTHOS))} "
                    f"bears_bf={len(bf_ids(s, 0, BEAR))} "
                    f"salves_hand={sum(1 for n in hand_lnames(s, 0) if n == SALVE)} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)}")
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
        for fn in ("pre", "offer1", "offer2", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, offer1, offer2, post = (states.get("pre"),
                                    states.get("offer1"),
                                    states.get("offer2"),
                                    states.get("post"))
        ST["zones_seen"] = sorted(ST["zones_seen"])

        def creatures_of(s):
            return [oid for oid in bf_ids(s, 0)
                    if lname(s, oid) in (NYKTHOS, BEAR)]

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
            c = cd["nykthos paragon"]
            t0 = (c.get("triggers") or [])[0]
            ex = t0.get("execute", {})
            eff = ex.get("effect", {})
            cnt = eff.get("count", {})
            tgt = eff.get("target", {})
            ok = (
                t0.get("mode") == "LifeGained"
                and eff.get("type") == "PutCounter"
                and eff.get("counter_type") == "P1P1"
                and cnt.get("type") == "Ref"
                and (cnt.get("qty") or {}).get("type")
                == "EventContextAmount"
                and tgt.get("type") == "Typed"
                and "Creature" in (tgt.get("type_filters") or [])
                and tgt.get("controller") == "You"
                and ex.get("optional") is True
                and (t0.get("constraint") or {}).get("type")
                == "OncePerTurn")
            notes.append(
                f"A1: mode={t0.get('mode')} effect={eff.get('type')} "
                f"counter={eff.get('counter_type')} "
                f"count={cnt.get('type')}/{ (cnt.get('qty') or {}).get('type')} "
                f"target={tgt.get('type')}/{tgt.get('type_filters')}/"
                f"{tgt.get('controller')} optional={ex.get('optional')} "
                f"constraint={(t0.get('constraint') or {}).get('type')} "
                f"batched={t0.get('batched')}; "
                f"OBS: 'each creature' is a Typed target with no explicit "
                f"all-matching marker")
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            n_par = len(bf_ids(pre, 0, NYKTHOS))
            n_bear = len(bf_ids(pre, 0, BEAR))
            salves = sum(1 for n in hand_lnames(pre, 0) if n == SALVE)
            ok = (n_par == 1 and n_bear >= 2 and salves >= 2
                  and ST["p0_life_pre"] == 20)
            notes.append(f"A2: paragon_bf={n_par} bears_bf={n_bear} "
                         f"salves_hand={salves} "
                         f"p0_life_pre={ST['p0_life_pre']} "
                         f"turn={pre.get('turn_number')}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: first lifegain raised the offer ----
        if ST["offer1_seen"]:
            ass["A3_offer1"] = "passed"
            notes.append(f"A3: offer1 seen kind={ST['offer1_kind']} "
                         f"iid={ST['offer1_iid']} declined="
                         f"{ST['offer1_declined']}")
        elif ST["salve1_resolved"]:
            ass["A3_offer1"] = "failed"
            notes.append("A3 FAILED: salve #1 resolved (P0 "
                         f"{ST['p0_life_pre']}->"
                         f"{ST['p0_life_post_salve1']}) but no Nykthos "
                         "optional prompt was ever raised for P0 "
                         f"(skipped={ST['offer1_skipped']})")
        else:
            ass["A3_offer1"] = "not-run"
            notes.append("A3 not-run: leg 1 never executed "
                         f"(salve1_cast={ST['salve1_cast']} "
                         f"resolved={ST['salve1_resolved']})")

        # ---- A4: decline preserved the later trigger ----
        if MODE == "accept":
            ass["A4_decline_preserves"] = "not-run"
            notes.append("A4 not-run: accept-only mode (decline preservation "
                         "is covered by the decline-mode run)")
        elif ST["offer2_seen"]:
            ass["A4_decline_preserves"] = "passed"
            notes.append(f"A4: offer2 seen kind={ST['offer2_kind']} "
                         f"iid={ST['offer2_iid']} accepted="
                         f"{ST['offer2_accepted']}")
        elif ST["salve2_resolved"]:
            ass["A4_decline_preserves"] = "failed"
            notes.append("A4 FAILED: salve #2 resolved (P0 "
                         f"{ST['p0_life_post_salve1']}->"
                         f"{ST['p0_life_post_salve2']}) after leg-1 decline, "
                         "but no Nykthos optional prompt was raised again "
                         f"(skipped={ST['offer2_skipped']}) -- the reported "
                         "'only offers once' symptom")
        else:
            ass["A4_decline_preserves"] = "not-run"
            notes.append("A4 not-run: leg 2 never executed "
                         f"(salve2_cast={ST['salve2_cast']} "
                         f"resolved={ST['salve2_resolved']})")

        # ---- A5: every creature got exactly 3 counters ----
        accepted = (ST["offer1_accepted"] if MODE == "accept"
                    else ST["offer2_accepted"])
        if post is not None and pre is not None and accepted:
            cres_pre = creatures_of(pre)
            cres_post = creatures_of(post)
            per = []
            all_ok = True
            for oid in cres_pre:
                nm = lname(pre, oid)
                c0, f0 = counters_of(pre, oid)
                p0v, t0v = pt_of(pre, oid)
                if oid in cres_post:
                    c1, f1 = counters_of(post, oid)
                    p1v, t1v = pt_of(post, oid)
                else:
                    c1, f1, p1v, t1v = None, False, None, None
                if f0 or f1:
                    delta = (c1 or 0) - (c0 or 0)
                    ok_c = (delta == 3)
                    per.append(f"{nm}#{oid}: counters {c0}->{c1} "
                               f"(delta={delta}, expect 3)")
                elif p0v is not None and p1v is not None:
                    # P/T fallback: nothing else modifies P/T in this game
                    dp = (p1v or 0) - (p0v or 0)
                    dt = (t1v or 0) - (t0v or 0)
                    ok_c = (dp == 3 and dt == 3)
                    per.append(f"{nm}#{oid}: P/T fallback {p0v}/{t0v}->"
                               f"{p1v}/{t1v} (delta=+{dp}/+{dt}, expect "
                               f"+3/+3)")
                else:
                    ok_c = False
                    per.append(f"{nm}#{oid}: no counter or P/T data")
                all_ok = all_ok and ok_c
            n_pre, n_post = len(cres_pre), len(cres_post)
            ok = all_ok and n_pre >= 3 and n_post == n_pre
            notes.append(f"A5: creatures pre/post={n_pre}/{n_post}; "
                         + "; ".join(per))
            if MODE == "accept" and ST["choice_seen"]:
                notes.append(f"A5 mechanism: after accepting, the engine "
                             f"raised ChooseFromZoneChoice count="
                             f"{ST['choice_count']} over "
                             f"{len(ST['choice_cards'])} creatures "
                             f"(source oid={ST['choice_source']}); driver "
                             f"chose oid={ST['choice_oid']}")
            if not ok and all_ok and n_post != n_pre:
                notes.append("A5 note: creature count changed pre->post")
        elif accepted:
            ok = False
            notes.append("A5 failed: post.json or pre.json missing")
        else:
            ok = None
            notes.append("A5 not-run: accept leg never completed "
                         f"(offer1_seen={ST['offer1_seen']} "
                         f"offer1_accepted={ST['offer1_accepted']} "
                         f"offer2_seen={ST['offer2_seen']} "
                         f"offer2_accepted={ST['offer2_accepted']})")
        ass["A5_counters_all"] = ("passed" if ok else
                                  ("failed" if ok is False else "not-run"))

        # ---- A6: cleanup ----
        exp_salves = 1 if MODE == "accept" else 2
        exp_life = 23 if MODE == "accept" else 26
        if post is not None:
            n_salve_gy = len(gy_oids(post, 0, SALVE))
            life0 = life_of(post, 0)
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            # post may land during a cleanup discard (hand >7 from draws);
            # that is normal game flow, not a stall. P0 also discards
            # spare Salves to hand size during the long settle windows,
            # so the graveyard can hold MORE than the cast copies.
            ok = (n_salve_gy >= exp_salves and life0 == exp_life
                  and stack_empty and wft in ("Priority",
                                              "DiscardToHandSize"))
            notes.append(f"A6: P0-gy salves={n_salve_gy} "
                         f"(at least the {exp_salves} cast copies) "
                         f"P0_life={life0} (expect {exp_life}) "
                         f"stack_empty={stack_empty} post_wf={wft}")
        else:
            ok = False
            notes.append("A6 failed: post.json missing")
        ass["A6_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif MODE == "accept":
            if ass["A3_offer1"] == "failed" or \
                    ass["A5_counters_all"] == "failed":
                verdict = "reproduced"
                notes.append("verdict=reproduced (accept mode): the offer "
                             "never came or counters did not land on every "
                             "creature")
            elif all(ass.get(k) == "passed"
                     for k in ("A2_setup", "A3_offer1", "A5_counters_all",
                               "A6_cleanup")):
                verdict = "not-reproduced"
                notes.append("verdict=not-reproduced (accept mode): offer "
                             "raised and every creature gained exactly 3 "
                             "counters")
            else:
                verdict = "blocked"
                notes.append("verdict=blocked: incomplete assertion chain")
        elif ass["A3_offer1"] == "failed" or \
                ass["A4_decline_preserves"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the decline-suppresses-future-"
                         "offers symptom (A4) or a missing first offer (A3) "
                         "failed; A5 not-run is a consequence of the bug "
                         "when the second offer never arrives")
        elif ass["A5_counters_all"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: accept applied counters to "
                         "fewer than all creatures")
        elif all(ass.get(k) == "passed"
                 for k in ("A2_setup", "A3_offer1", "A4_decline_preserves",
                           "A5_counters_all", "A6_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: decline preserved the "
                         "later offer and every creature gained exactly 3 "
                         "counters")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict} mode={MODE}")

        run = {
            "run_id": RUN_ID, "issue": 7142,
            "verdict": verdict, "validated_at": "2026-09-14",
            "mode": MODE,
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7142.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (list(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "offer1.json", "offer2.json",
                               "choice.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7142.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x/18x card density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "P1 is a fully passive punching bag (60x Forest; never "
                "plays lands, never casts, never attacks, never blocks).",
                "No combat occurs; life changes come only from the two "
                "Healing Salves, so lifegain events are fully controlled.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7142.py",
                    f"{EVDIR}/scenario_7142.py")
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
        W, H = 1000, 1100
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7142 - Nykthos Paragon",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-14 - "
               "optional trigger + once-per-turn + each-creature counters",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: whenever you gain life, you may put that",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "many +1/+1 counters on each creature you control.",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "Do this only once each turn.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Line: Healing Salve (gain 3) in one turn; " +
               ("decline the offer, then check it returns." if MODE == "decline"
                else "accept the offer; check every creature."),
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: LifeGained -> P1P1 x gained, optional, 1/turn",
            "A2_setup": "PRE: paragon + 2 bears on BF, 2 salves, P0 at 20",
            "A3_offer1": "lifegain #1 raised the Nykthos offer",
            "A4_decline_preserves": "decline did not consume; offer #2 raised",
            "A5_counters_all": "accept: EVERY creature +3 counters",
            "A6_cleanup": ("1 salve in gy, P0 at 23, stack empty"
                           if MODE == "accept" else
                           "2 salves in gy, P0 at 26, stack empty"),
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((40, y), f"{k}: {v}", fill=col)
            d.text((300, y), lab, fill=(180, 190, 205))
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {})
        lines = [
            f"leg1: salve1 cast={ds.get('salve1_cast')} "
            f"resolved={ds.get('salve1_resolved')} "
            f"P0 {ds.get('p0_life_pre')}->"
            f"{ds.get('p0_life_post_salve1')}",
            f"offer1: seen={ds.get('offer1_seen')} kind="
            f"{ds.get('offer1_kind')} declined="
            f"{ds.get('offer1_declined')} skipped="
            f"{ds.get('offer1_skipped')}",
            f"leg2: salve2 cast={ds.get('salve2_cast')} "
            f"resolved={ds.get('salve2_resolved')} "
            f"P0 {ds.get('p0_life_post_salve1')}->"
            f"{ds.get('p0_life_post_salve2')}",
            f"offer2: seen={ds.get('offer2_seen')} kind="
            f"{ds.get('offer2_kind')} accepted="
            f"{ds.get('offer2_accepted')} skipped="
            f"{ds.get('offer2_skipped')}",
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
        files = ["pre.json", "offer1.json", "offer2.json", "choice.json",
                 "post.json", "run.json", "scenario_7142.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]

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
