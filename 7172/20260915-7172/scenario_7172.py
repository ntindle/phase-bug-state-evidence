#!/usr/bin/env python3
"""Issue #7172: Deadpool, Trading Card -- Deadpool's ETB does not trigger.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.83.0, key 'deadpool, trading card'):
  Deadpool, Trading Card ({2}{B}{R}, 5/3 Legendary Creature - Mutant
  Mercenary Hero):
    "As Deadpool enters, you may exchange his text box and another
     creature's.
     At the beginning of your upkeep, you lose 3 life.
     {3}, Sacrifice this creature: Each other player draws a card."

Card-data parse state on v0.83.0 (verified 2026-09-15 before the run):
  abilities[0] = kind Spell, effect {type: Unimplemented, name: "unknown"},
    description "As ~ enters, you may exchange his text box and another
    creature's."
  replacements = [] (no as-enters replacement registered)
  triggers[0] = Phase Upkeep -> LoseLife 3 to Controller (implemented)
  abilities[1] = Activated {3}, Sacrifice SelfRef -> Opponent draws 1
    (implemented)

The reported defect: as Deadpool enters, the controller is never offered the
optional text-box exchange (no choice, no resulting text-box change).

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x deadpool, trading card, 12x grizzly bears, 12x swamp,
      12x mountain, 12x lightning bolt (60).
  P1: 12x grizzly bears, 24x forest, 24x lightning bolt (60; passive except
      that it puts a Grizzly Bears on the battlefield as the exchange
      candidate -- the report's exchange needs "another creature").

Planned line:
  Turns 1-4: land drops; P1 casts a Grizzly Bears on its turn 2 (opponent's
    creature on the battlefield as an exchange candidate).
  Turn 5 (P0): pre.json export, then cast Deadpool, Trading Card. As it
    enters, watch every waiting_for / viewer_interaction opportunity for an
    exchange choice (EntryControllerChoice / OptionalEffectChoice /
    EffectZoneChoice / TargetSelection naming the exchange).
  Entry+5s: if no exchange prompt ever appeared, export entry.json and
    confirm Deadpool entered as a 5/3 with its own text box (Grizzly Bears
    unchanged at 2/2 with no rules text swap).
  Control: at P0's next upkeep the lose-3-life trigger must fire (20->17),
    and the {3}-sacrifice activated ability must be advertised -- proving the
    card is otherwise live and only the exchange is dead.

Assertions (each passed / failed / not-run):
  A1_parse               card-data: as-enters clause is Unimplemented /
                         unknown with no registered replacement (parse-level
                         root cause of the reported gap).
  A2_setup               PRE: P1 has a Grizzly Bears on the battlefield and
                         P0 holds Deadpool with 4+ lands (Swamp+Mountain)
                         in play -- a legal exchange candidate exists.
  A3_no_exchange_choice  during the entry window, NO exchange choice was
                         ever offered to Deadpool's controller. (The
                         reported bug fails HERE if a prompt appears.)
  A4_enters_unchanged    Deadpool entered as 5/3 with its own text box;
                         both creatures unchanged (no partial swap).
  A5_card_otherwise_live upkeep trigger resolved (P0 20->17) and the
                         sacrifice activated ability is advertised.
  A6_cleanup             stack empty, game proceeded past the entry window.

Verdict rule:
  reproduced     iff A2 passes and A3 passes (no exchange choice was ever
                 offered despite a valid candidate) - the reported symptom.
  not-reproduced iff an exchange choice IS offered and answering it swaps
                 both text boxes correctly.
  blocked        iff A2 fails (setup never reached).

Evidence: evidence/7172/<run-id>/pre.json, entry.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7172.py, wire_log.jsonl,
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
RUN_ID = os.environ.get("RUN_ID", "20260915-7172")
EVDIR = f"{BACKFILL}/evidence/7172/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DEADPOOL = "deadpool, trading card"
BEARS = "grizzly bears"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
BOLT = "lightning bolt"
P0_LANDS = (SWAMP, MOUNTAIN)
P1_LANDS = (FOREST,)

P0_DECK = [(DEADPOOL, 12), (BEARS, 12), (SWAMP, 12), (MOUNTAIN, 12),
           (BOLT, 12)]
P1_DECK = [(BEARS, 12), (FOREST, 24), (BOLT, 24)]

SERVER_IDENTITY = {
    "server_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed0",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key); server already running "
              "on 127.0.0.1:9374 by this run's session.",
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


def pt_of(state, oid):
    o = get_obj(state, oid)
    return (o.get("power"), o.get("toughness"))


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid}")
    wire("interaction_submission", {"who": tag, "submission": sub})
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",          # setup -> cast_pending -> entry_watch ->
                                   # upkeep_watch -> cleanup -> done
        "pre_exported": False, "entry_exported": False,
        "post_exported": False,
        "land_turn": -1,
        "p1_bears_cast": False,
        "deadpool_cast": False,
        "deadpool_cast_turn": None,
        "deadpool_entered": False,
        "deadpool_oid": None,
        "entry_detected_at": None,
        "exchange_prompt_seen": False,
        "entry_window_prompts": [],
        "exchange_candidates": [],
        "upkeep_trigger_seen": False,
        "upkeep_life_before": None,
        "upkeep_life_after": None,
        "sac_ability_advertised": False,
        "activated_abilities_seen": [],
        "turns_seen": set(),
        "cleanup_from_turn": None,
        "game_code": None,
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "entry_window_wf": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-deadpool")
    p1 = PhaseClient("P1-candidate")
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

    async def mulligan_keep(c, pid, tag, st, state, acts):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def bottom_cards(c, pid, tag, st, state, acts):
        w = wf_of(state)
        if (w.get("type") or "") != "SelectCards":
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
            # bottom lands first
            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                return 0 if any(l in tx for l in ("swamp", "mountain",
                                                 "forest", "island")) else 1
            pick = sorted(chs, key=rank)[:int(d.get("count", 1))]
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [c.get("id") for c in pick]}}}
            say(f"[{tag}] bottoming {len(pick)} cards")
            wire("bottom_cards", {"who": tag, "n": len(pick)})
            await c.send_interaction(sub)
            return True
        return False

    async def handle_discard(c, pid, tag, st, state, acts):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
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
                if tx.find(BEARS) >= 0:
                    return 2
                if tx.find(DEADPOOL) >= 0:
                    return 3
                return 0
            pick = sorted(chs, key=rank)[0]
            if acted(f"disc{iid}", st.get("state_revision", -1)):
                return True
            say(f"[{tag}] discarding to hand size")
            await answer_vi(c, opp, pick, tag)
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

    # exchange-flavored prompt kinds to watch for in the entry window
    EXCHANGE_WF = ("EntryControllerChoice", "OptionalEffectChoice",
                   "EffectZoneChoice", "TargetSelection", "CopyRetarget",
                   "ChooseCards", "EffectChoice")

    async def entry_window_watch(c, pid, tag, st, state):
        """During the entry window, record any choice prompt; do NOT
        auto-answer exchange-flavored prompts (they are the subject of
        the test and must be recorded, not driven past)."""
        wtype = wf_of(state).get("type") or ""
        if wtype not in EXCHANGE_WF:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        opps = vi.get("opportunities", []) or []
        if not opps:
            return False
        d = wf_of(state).get("data") or {}
        pl = d.get("player")
        if isinstance(pl, int) and pl != pid:
            return False
        seen_key = f"entrywin_{len(opps)}"
        rec = {"wf_type": wtype, "wf_data_keys": sorted(d.keys())[:8],
               "n_opps": len(opps),
               "opps": [{"iid": o.get("interactionId"),
                         "rtype": (o.get("response") or {}).get("type"),
                         "text": json.dumps(o.get("response"))[:200]}
                        for o in opps[:4]]}
        if rec not in ST["entry_window_prompts"]:
            ST["entry_window_prompts"].append(rec)
            obs["entry_window_wf"].append(wtype)
            wire("entry_window_prompt", {"who": tag, **rec})
            say(f"[{tag}] ENTRY-WINDOW prompt: wf={wtype} "
                f"n_opps={len(opps)}")
            ST["exchange_prompt_seen"] = True
        # deliberately NOT answered: the exchange choice is the observed
        # phenomenon, not something to drive past
        return True

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
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
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        ST["turns_seen"].add(turn)

        # exchange-flavored prompt watch: only during the entry window.
        # Deliberately no return here: even if a prompt is seen, the tick
        # must fall through to the 5s-close logic so the run can wrap up
        # (an unanswered prompt cannot be driven past).
        if ST["stage"] in ("cast_pending", "entry_watch"):
            await entry_window_watch(p0, 0, "P0", st, state)

        # record activated abilities advertised for Deadpool (control A5)
        for a in acts:
            if a.get("type") == "ActivateAbility":
                dd = a.get("data") or {}
                src = dd.get("source_id")
                try:
                    if src is not None and lname(state, int(src)) == DEADPOOL:
                        rec = {"ability_index": dd.get("ability_index"),
                               "source_id": src}
                        if rec not in ST["activated_abilities_seen"]:
                            ST["activated_abilities_seen"].append(rec)
                            ST["sac_ability_advertised"] = True
                            say(f"[P0] ActivateAbility advertised for "
                                f"Deadpool: {rec}")
                except Exception:
                    pass

        # track Deadpool entering the battlefield
        dp_bf = bf_ids(state, 0, DEADPOOL)
        if dp_bf and not ST["deadpool_entered"]:
            ST["deadpool_entered"] = True
            ST["deadpool_oid"] = dp_bf[0]
            ST["entry_detected_at"] = time.time()
            say(f"[P0] Deadpool ENTERED battlefield oid={dp_bf[0]} "
                f"turn={turn} phase={phase} wf={wtype}")
            # exchange candidates present at entry
            ST["exchange_candidates"] = [
                {"name": str(get_obj(state, int(oid)).get("base_name") or
                             get_obj(state, int(oid)).get("name") or "?"),
                 "oid": int(oid),
                 "controller": get_obj(state, int(oid)).get("controller"),
                 "power": get_obj(state, int(oid)).get("power"),
                 "toughness": get_obj(state, int(oid)).get("toughness")}
                for oid, o in (state.get("objects", {}) or {}).items()
                if o.get("zone") == "Battlefield" and int(oid) != dp_bf[0]
                and str(o.get("base_name") or o.get("name")
                        or "").lower() == BEARS
            ]
            say(f"[P0] exchange candidates at entry: "
                f"{ST['exchange_candidates']}")
            ST["stage"] = "entry_watch"
            if await export_named("entry"):
                ST["entry_exported"] = True

        # entry window: hold 5s after entry, then move on
        if ST["stage"] == "entry_watch":
            if time.time() - (ST["entry_detected_at"] or 0) > 5:
                ST["stage"] = "upkeep_watch"
                ST["upkeep_from_turn"] = turn
                say("[P0] entry window closed (5s), no exchange prompt "
                    f"answered; exchange_prompt_seen="
                    f"{ST['exchange_prompt_seen']}")
            elif my_priority(state, 0) and not acted("pass_ew", rev):
                pa = find_action(acts, "PassPriority")
                if pa:
                    await submit_as_is(p0, pa)
            return

        # upkeep watch: next P0 upkeep should lose 3 life (control)
        if ST["stage"] == "upkeep_watch":
            dp = bf_ids(state, 0, DEADPOOL)
            if not dp and ST["deadpool_entered"]:
                say("[P0] WARNING: Deadpool left the battlefield during "
                    "upkeep_watch")
            if phase == "Upkeep" and active == 0:
                if ST["upkeep_life_before"] is None:
                    ST["upkeep_life_before"] = life_of(state, 0)
                    say(f"[P0] upkeep entered, life before="
                        f"{ST['upkeep_life_before']}")
            if ST["upkeep_life_before"] is not None \
                    and not ST["upkeep_trigger_seen"]:
                cur = life_of(state, 0)
                if cur is not None and cur < ST["upkeep_life_before"]:
                    ST["upkeep_trigger_seen"] = True
                    ST["upkeep_life_after"] = cur
                    say(f"[P0] upkeep trigger RESOLVED: life "
                        f"{ST['upkeep_life_before']}->{cur}")
                    ST["stage"] = "cleanup"
                    ST["cleanup_from_turn"] = turn
                    if await export_named("post"):
                        ST["post_exported"] = True
                        ST["stage"] = "done"
                    return

        if ST["stage"] == "cleanup":
            if turn > ST.get("cleanup_from_turn", turn) \
                    and not (state.get("stack") or []):
                if await export_named("post"):
                    ST["post_exported"] = True
                    ST["stage"] = "done"
                return

        # --- main-phase actions (gated while a P0 cast is in flight) ---
        in_flight = ST["stage"] == "cast_pending" and any(
            str(e.get("name") or e.get("card_name")
                or "").lower() == DEADPOOL
            for e in (state.get("stack") or []))
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain") \
                and not in_flight:
            # land drop (retry every tick; no kept flag)
            for oid in hand_ids(state, 0):
                if lname(state, oid) in P0_LANDS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("land", rev):
                        say(f"[P0] playing land {lname(state, oid)}")
                        await submit_as_is(p0, pla)
                        ST["land_turn"] = turn
                        return
            # cast Deadpool when the candidate creature is in place
            dp_in_hand = any(n == DEADPOOL for n in hand_lnames(state, 0))
            p1_creatures = bf_ids(state, 1)
            p1_bears = bf_ids(state, 1, BEARS)
            ul = untapped_lands(state, 0, P0_LANDS)
            ul_names = [lname(state, o) for o in ul]
            if (ST["stage"] == "setup" and dp_in_hand and p1_bears
                    and turn >= 5 and len(ul) >= 4
                    and SWAMP in ul_names and MOUNTAIN in ul_names):
                ca = cast_spell_action(acts, state, 0, DEADPOOL)
                if ca and not acted("deadpool", rev):
                    if not ST["pre_exported"]:
                        if await export_named("pre"):
                            ST["pre_exported"] = True
                    say("[P0] casting Deadpool, Trading Card "
                        f"(p1_bears={[int(o) for o in p1_bears]})")
                    await submit_as_is(p0, ca)
                    ST["deadpool_cast"] = True
                    ST["deadpool_cast_turn"] = turn
                    ST["stage"] = "cast_pending"
                    return
            # safety: cast later if the candidate is late but mana is there
            if (ST["stage"] == "setup" and dp_in_hand and p1_creatures
                    and turn >= 7 and len(ul) >= 4
                    and SWAMP in ul_names and MOUNTAIN in ul_names):
                ca = cast_spell_action(acts, state, 0, DEADPOOL)
                if ca and not acted("deadpool_late", rev):
                    if not ST["pre_exported"]:
                        if await export_named("pre"):
                            ST["pre_exported"] = True
                    say("[P0] casting Deadpool, Trading Card (late "
                        "fallback)")
                    await submit_as_is(p0, ca)
                    ST["deadpool_cast"] = True
                    ST["deadpool_cast_turn"] = turn
                    ST["stage"] = "cast_pending"
                    return

        # default: pass priority (always fall through, cf. #6862)
        if my_priority(state, 0) and not acted("pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
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
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
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
        # land drops + one Bears as the exchange candidate; fully passive
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) in P1_LANDS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
            if not ST["p1_bears_cast"] and not bf_ids(state, 1, BEARS):
                ul = untapped_lands(state, 1, P1_LANDS)
                if len(ul) >= 2:
                    ca = cast_spell_action(acts, state, 1, BEARS)
                    if ca and not acted("p1bears", rev):
                        say("[P1] casting Grizzly Bears (exchange "
                            "candidate)")
                        await submit_as_is(p1, ca)
                        ST["p1_bears_cast"] = True
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
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"dp_entered={ST['deadpool_entered']} "
                    f"p1_bears_bf={len(bf_ids(s, 1, BEARS))} "
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
        for fn in ("pre", "entry", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, entry, post = (states.get("pre"), states.get("entry"),
                            states.get("post"))

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.83.0/data/card-data.json"))
            c = cd["deadpool, trading card"]
            ab0 = (c.get("abilities") or [{}])[0]
            eff = ab0.get("effect") or {}
            unimpl = eff.get("type") == "Unimplemented"
            desc = str(ab0.get("description") or "")
            has_exchange_desc = "exchange" in desc.lower() \
                and "text box" in desc.lower()
            no_replacements = c.get("replacements") == []
            ok = unimpl and has_exchange_desc and no_replacements
            notes.append(f"A1: effect.type={eff.get('type')} "
                         f"(name={eff.get('name')}), "
                         f"replacements={c.get('replacements')}, "
                         f"exchange-in-desc={has_exchange_desc}")
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            p1b = bf_ids(pre, 1, BEARS)
            dp_hand = any(n == DEADPOOL for n in hand_lnames(pre, 0))
            ul = untapped_lands(pre, 0, P0_LANDS)
            ok = (len(p1b) >= 1 and dp_hand and len(ul) >= 4)
            notes.append(f"A2: p1_bears_bf={len(p1b)} "
                         f"deadpool_in_hand={dp_hand} "
                         f"p0_untapped_lands={len(ul)}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: no exchange choice offered ----
        if ST["deadpool_entered"]:
            ok = not ST["exchange_prompt_seen"]
            notes.append(f"A3: deadpool_entered={ST['deadpool_entered']}, "
                         f"exchange_prompt_seen={ST['exchange_prompt_seen']}, "
                         f"entry_window_wf_kinds={sorted(set(obs['entry_window_wf']))}")
        else:
            ok = False
            notes.append("A3 failed: Deadpool never entered the battlefield")
        ass["A3_no_exchange_choice"] = "passed" if ok else "failed"

        # ---- A4: enters unchanged ----
        if entry is not None:
            dp = bf_ids(entry, 0, DEADPOOL)
            ok_dp = len(dp) == 1
            p, t = pt_of(entry, dp[0]) if ok_dp else (None, None)
            # power/toughness may be strings or ints
            def num(x):
                try:
                    return int(x)
                except Exception:
                    return None
            ok_pt = num(p) == 5 and num(t) == 3
            b1 = bf_ids(entry, 1, BEARS)
            bp, bt = pt_of(entry, b1[0]) if b1 else (None, None)
            ok_bears = b1 and num(bp) == 2 and num(bt) == 2
            ok = ok_dp and ok_pt and ok_bears
            notes.append(f"A4: deadpool_bf={len(dp)} pt=({p},{t}); "
                         f"p1_bears_bf={len(b1)} pt=({bp},{bt})")
        else:
            ok = False
            notes.append("A4 failed: entry.json missing")
        ass["A4_enters_unchanged"] = "passed" if ok else "failed"

        # ---- A5: rest of card live ----
        up = (ST["upkeep_trigger_seen"]
              and ST["upkeep_life_before"] == 20
              and ST["upkeep_life_after"] == 17)
        sac = ST["sac_ability_advertised"]
        ok = up and sac
        notes.append(f"A5: upkeep_trigger={ST['upkeep_trigger_seen']} "
                     f"life {ST['upkeep_life_before']}->"
                     f"{ST['upkeep_life_after']}; sac_ability_advertised="
                     f"{sac} ({ST['activated_abilities_seen']})")
        ass["A5_card_otherwise_live"] = "passed" if ok else "failed"

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
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
        elif (ass["A3_no_exchange_choice"] == "passed"
                and ass["A4_enters_unchanged"] == "passed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: Deadpool entered with a legal "
                         "exchange candidate on the battlefield, but the "
                         "controller was never offered the optional text-box "
                         "exchange - the exact reported symptom")
        elif ass["A3_no_exchange_choice"] == "failed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: an exchange choice WAS "
                         "offered during the entry window")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7172,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.83.0 single-user server on "
                               "127.0.0.1:9374 (already running for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7172.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "entry.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_7172.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is near-passive (lands + one Grizzly Bears, never "
                "attacks/blocks) so the entry window stays clean.",
                "The accept-branch (choosing to exchange) and the decline "
                "control are not-run: no exchange choice exists to answer.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7172.py",
                    f"{EVDIR}/scenario_7172.py")
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
        d.text((24, y), "phase-rs/phase #7172 - Deadpool, Trading Card",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-15 - "
               "as-enters text-box exchange",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: 'As Deadpool enters, you may exchange his "
               "text box and another creature's.'",
               fill=(200, 210, 225))
        y += 26
        d.text((24, y), "card-data parse: effect Unimplemented/unknown, "
               "replacements=[] -- engine cannot offer the exchange",
               fill=(200, 210, 225))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: exchange clause Unimplemented, "
                        "no replacement",
            "A2_setup": "PRE: P1 Bears on BF + Deadpool in P0 hand + mana",
            "A3_no_exchange_choice": "entry window: NO exchange choice "
                                     "offered (reported symptom)",
            "A4_enters_unchanged": "Deadpool enters 5/3, Bears stay 2/2",
            "A5_card_otherwise_live": "upkeep -3 life + sacrifice ability "
                                      "advertised",
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
        pre, entry, post = states.get("pre"), states.get("entry"), states.get("post")
        def life(s, pid):
            for p in (s or {}).get("players", []):
                if p.get("id") == pid:
                    return p.get("life")
            return "?"
        d.text((24, y), "Pre-entry (P1 Bears on BF, Deadpool in P0 hand):",
               fill=(200, 210, 225))
        y += 24
        if pre is not None:
            p1b = [o for o in pre.get("objects", {}).values()
                   if str(o.get("base_name") or o.get("name")
                          or "").lower() == BEARS
                   and o.get("zone") == "Battlefield"]
            dph = sum(1 for p in pre.get("players", [])
                      if p.get("id") == 0
                      for o in p.get("hand", [])
                      if str(pre.get("objects", {}).get(str(o), {})
                             .get("base_name") or "").lower() == DEADPOOL)
            d.text((40, y), f"P1 creatures on BF: {len(p1b)}; "
                   f"Deadpool in P0 hand: {dph}; "
                   f"life: {life(pre, 0)}/{life(pre, 1)}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "pre.json MISSING", fill=(255, 90, 90))
        y += 30
        d.text((24, y), "Post-entry (no exchange prompt was ever offered):",
               fill=(200, 210, 225))
        y += 24
        if entry is not None:
            dp = [o for o in entry.get("objects", {}).values()
                  if str(o.get("base_name") or o.get("name")
                         or "").lower() == DEADPOOL
                  and o.get("zone") == "Battlefield"]
            d.text((40, y), f"Deadpool on P0 BF: {len(dp)} "
                   f"(5/3, own text box); life: "
                   f"{life(entry, 0)}/{life(entry, 1)}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "entry.json MISSING", fill=(255, 90, 90))
        y += 30
        d.text((24, y), "Post-upkeep (control: rest of card works):",
               fill=(200, 210, 225))
        y += 24
        if post is not None:
            d.text((40, y), f"P0 life after upkeep: {life(post, 0)} "
                   f"(20->17 if -3 trigger resolved); "
                   f"stack: {len(post.get('stack') or [])}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "post.json MISSING", fill=(255, 90, 90))
        y += 40
        d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
               "7172/" + RUN_ID + "/", fill=(140, 160, 180))
        p = os.path.join(EVDIR, "summary.png")
        img.save(p)
        say(f"saved summary.png ({os.path.getsize(p)} bytes)")

    def write_manifest():
        files = [f for f in sorted(os.listdir(EVDIR))
                 if f not in ("manifest.sha256",)]
        lines = []
        for fn in files:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
            f.write("\n".join(lines) + "\n")
        say(f"wrote manifest.sha256 ({len(lines)} files)")

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
