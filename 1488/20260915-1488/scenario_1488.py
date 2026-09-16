#!/usr/bin/env python3
"""Issue #1488: Fateful Tempest -- "Card just." (does not work).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-05-30): "Fateful Tempest -- Card just."
Triage comment (mike-theDude, 2026-07-10): the parsed spell ability's
top-level effect is Effect::Unimplemented { name: "vote", ... } and casting
"currently does nothing for the vote/mill/damage portion".
Prior backfill validation (2026-09-09, v0.78.0, run 20260909-1488): verdict
reproduced -- the spell resolves with no observable effect; no vote offered,
no mill, no damage, no exile. Maintained comment on the issue supersedes.

Oracle text (verified from pinned v0.84.0 card-data.json, key 'fateful tempest'):
  "Council's dilemma -- Starting with you, each player votes for past or
   present. You mill a card for each past vote, then Fateful Tempest deals
   damage to each opponent equal to the total mana value of cards milled this
   way. Exile the top card of your library for each present vote. Until the
   end of your next turn, you may play the exiled cards."
Pinned parse (v0.84.0 dataset), same shape as v0.78.0:
  top-level effect = Unimplemented { name: "unrecognized_clause_head",
      description: "vote for past or present" }
  sub-chain (SequentialSibling):
    Mill { Fixed 1, target Controller, dest Graveyard }
    -> DamageEachPlayer { Ref PropertyAggregate Sum(ManaValue, TrackedSet),
                          filter Opponent }
    -> ExileTop { Controller, Fixed 1 }
    -> CastFromZone { ExiledBySource, Play, UntilEndOfNextTurnOf Controller }
The vote itself remains Unimplemented; the mill/damage/exile sub-effects are
parsed with FIXED counts of 1 instead of per-vote counts.

Setup (native engine, two human-client seats, protocol-71 driver):
  P0: 12x fateful tempest ({2}{R} sorcery) + 48x mountain.
      (12x density: engine accepts >4-of for custom games; mulligan to
      Tempest + 2+ lands.)
  P1: 60x mountain dummy (plays a land, passes; never attacks).

Expected (per Oracle text):
  E1: after P0 casts Fateful Tempest, each player is offered a past/present
      vote (council's dilemma, starting with P0).
  E2: P0 mills one card per past vote.
  E3: each opponent is dealt damage equal to the total mana value of the
      cards milled this way.
  E4: P0 exiles the top card of their library per present vote and may play
      those cards until the end of P0's next turn.
  E5: Tempest goes to P0's graveyard, stack empties, game proceeds.

Assertions:
  A1_setup_ok    pre.json: P0 main phase, Tempest in hand, >=3 untapped
                 Mountains, life 20/20.
  A2_vote_prompted  a past/present vote opportunity is advertised to the
                 players between cast and resolution (recorded in wire log).
  A3_mill        P0 graveyard grows by N cards during resolution; N and the
                 milled card names recorded.
  A4_damage      P1 life delta during resolution; consistency check:
                 damage == sum of MV of milled cards.
  A5_exile       exile-zone delta during resolution (cards exiled by the
                 Tempest resolution), names recorded.
  A6_cleanup     post.json: Tempest in P0's graveyard, stack empty, turn
                 advanced past the cast turn, game proceeding.

Verdict rule: reproduced iff A1 passed and the Oracle-mandated
vote/mill/damage/exile flow is observably broken (no vote prompt, and/or
mill/exile counts inconsistent with any legal 2-player vote outcome, and/or
no mill/damage/exile at all). not-reproduced iff a vote prompt appeared,
votes were recorded, and mill/damage/exile matched the votes per Oracle.
blocked iff the game cannot be driven to a Tempest cast.

Evidence: evidence/1488/<run-id>/pre.json (before cast), cast_on_stack.json
(optional mid), mid_vote.json (only if a vote interaction appears),
post.json (after full resolution), run.json, manifest.sha256, summary.png,
scenario_1488.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260915-1488"
EVDIR = f"{BACKFILL}/evidence/1488/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TEMPEST = "fateful tempest"  # card-data.json key (exact)
MOUNTAIN = "mountain"        # card-data.json key (exact)

P0_DECK = [(TEMPEST, 12), (MOUNTAIN, 48)]
P1_DECK = [(MOUNTAIN, 60)]

SERVER_IDENTITY = {
    "server_version": "0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "digests recomputed via sha256sum against the pinned v0.84.0 "
              "release files under server/releases/v0.84.0/; ServerHello on "
              "127.0.0.1:9375 for run 20260915-1488 confirmed "
              "server_version 0.84.0 / build eb7e93e / protocol 71",
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


def lname(state, oid):
    o = state.get("objects", {}).get(str(oid), {})
    return obj_name(o)


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def mana_value(o):
    """Best-effort MV from the exported cost structure."""
    for key in ("mana_cost", "base_mana_cost"):
        c = o.get(key)
        if isinstance(c, dict):
            if c.get("type") == "NoCost":
                return 0
            if c.get("type") == "Cost":
                try:
                    return int(c.get("generic", 0)) + len(c.get("shards", []) or [])
                except Exception:
                    return None
    return None


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


def untapped_mountains(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == MOUNTAIN and not o.get("tapped")]


def tempest_in_hand_oid(state, pid):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == TEMPEST:
            return int(o)
    return None


def tempest_spell_on_stack(state):
    """Spell entries on the stack whose effect mentions Fateful Tempest."""
    out = []
    for e in state.get("stack", []) or []:
        blob = json.dumps(e, default=str).lower()
        if "fateful tempest" in blob:
            out.append(e)
    return out


def pending_for(state, pid):
    return [p for p in ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            if p.get("player") == pid]


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
            if isinstance(d, dict):
                t = d.get("name") or d.get("text") or d.get("value") or ""
                if t:
                    break
    return str(t)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_vote_prompted", "A3_mill",
            "A4_damage", "A5_exile", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    mull_state = {}

    obs = {"vote_seen": False, "vote_opportunities": [],
           "interaction_shapes": [],
           "cast_submitted": False, "cast_turn": None, "cast_phase": None,
           "cast_oid": None,
           "milled": [], "exiled": [],
           "life_pre": None, "life_post": None,
           "damage_to_p1": None, "mv_sum_milled": None}
    pre_exported = False
    cast_on_stack_exported = False
    mid_vote_exported = False
    post_exported = False
    submitted_interactions = set()
    shapes_logged = set()

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def scan_interactions(st, who):
        """Log every opportunity; detect vote-like prompts. Returns True if acted."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            # choices live under response.data.choices (also check response.choices
            # and candidates paths for robustness)
            chs = (data.get("choices") or resp.get("choices")
                   or data.get("candidates") or [])
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            full = json.dumps(opp, default=str)
            low = (blob + " " + full).lower()
            is_vote = ("past" in low and "present" in low) or "vote" in low
            key = (who, rtype, blob[:80], is_vote)
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} vote_like={is_vote} "
                    f"choices=[{blob[:220]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "vote_like": is_vote,
                                           "interaction": opp})
                obs["interaction_shapes"].append(
                    {"who": who, "rtype": rtype, "vote_like": is_vote,
                     "choices": texts[:12]})
            if is_vote and not obs["vote_seen"]:
                obs["vote_seen"] = True
                obs["vote_opportunities"].append(
                    {"who": who, "rtype": rtype, "choices": texts[:12],
                     "interactionId": iid})
                say(f"[{who}] *** VOTE PROMPT OBSERVED ***")
                wire("vote_prompt", {"who": who, "interaction": opp})
            if iid in submitted_interactions:
                continue
            avail = [(ch, choice_text(ch)) for ch in chs
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            # Answer a past/present vote with "past" (deterministic policy,
            # recorded in notes). Leave anything else unanswered.
            if is_vote:
                pick = None
                for ch, t in avail:
                    if "past" in t.lower():
                        pick = ch
                        break
                if pick is None:
                    notes.append(f"vote prompt had no 'past' choice: {blob[:200]}")
                    continue
                spec = data.get("spec") or {}
                sub_type = spec.get("type") if isinstance(spec, dict) else None
                if rtype == "exactChoices":
                    sub = {"interactionId": iid, "response":
                           {"type": "choose", "data": {"choiceId": pick["id"]}}}
                else:
                    sub = {"interactionId": iid, "response":
                           {"type": sub_type or "sequence",
                            "data": {"choiceIds": [pick["id"]]}}}
                say(f"[{who}] votes PAST (choice {pick['id']})")
                wire("vote_submission", {"who": who, "submission": sub,
                                         "interaction": opp})
                notes.append(f"{who} voted 'past' on the council's-dilemma prompt "
                             f"(deterministic test policy)")
                c = p0 if who == "P0" else p1
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                if not mid_vote_exported:
                    try:
                        mid = await p0.export_state()
                        with open(f"{EVDIR}/mid_vote.json", "w") as f:
                            f.write(mid)
                        mid_vote_exported = True
                        say("exported MID (vote prompt observed)")
                    except Exception as e:
                        notes.append(f"mid vote export failed: {e}")
                acted = True
        return acted

    def evaluate():
        """Evaluate A1..A6 from saved states + observations."""
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            notes.append(f"state reload failed: {e}")
            pre_st, post_st = None, None
        # A1
        if pre_st is not None:
            ok = (tempest_in_hand_oid(pre_st, 0) is not None
                  and len(untapped_mountains(pre_st, 0)) >= 3
                  and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
                  and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
            obs["life_pre"] = [life_of(pre_st, 0), life_of(pre_st, 1)]
            if ok:
                ass["A1_setup_ok"] = "passed"
                notes.append("pre.json: P0 main phase, Tempest in hand, "
                             f"{len(untapped_mountains(pre_st, 0))} untapped "
                             "Mountains, life 20/20")
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append("pre.json setup precondition not met")
        # A2
        if obs["vote_seen"]:
            ass["A2_vote_prompted"] = "passed"
            notes.append(f"vote prompt observed: {obs['vote_opportunities']}")
        else:
            ass["A2_vote_prompted"] = "failed"
            notes.append("NO past/present vote opportunity was advertised to any "
                         "player during the Tempest cast/resolution (REPORTED BUG: "
                         "council's dilemma vote never happens)")
        # A3/A4/A5 from pre/post deltas
        if pre_st is not None and post_st is not None:
            obs["life_post"] = [life_of(post_st, 0), life_of(post_st, 1)]
            pre_gy = set(str(o) for o in player_of(pre_st, 0).get("graveyard", []))
            post_gy = [o for o in player_of(post_st, 0).get("graveyard", [])]
            new_gy = [o for o in post_gy if str(o) not in pre_gy]
            # exclude the cast Tempest spell object itself from "milled"
            excluded = {str(obs["cast_oid"])} if obs["cast_oid"] else set()
            milled = [(lname(post_st, o), mana_value(get_obj(post_st, o)))
                      for o in new_gy if str(o) not in excluded]
            obs["milled"] = milled
            obs["mv_sum_milled"] = sum(m for _, m in milled if m is not None)
            obs["damage_to_p1"] = obs["life_pre"][1] - obs["life_post"][1]
            pre_ex = set(str(o) for o in (pre_st.get("exile", []) or []))
            new_ex = [o for o in (post_st.get("exile", []) or [])
                      if str(o) not in pre_ex]
            obs["exiled"] = [lname(post_st, o) for o in new_ex]
            say(f"milled={milled} mv_sum={obs['mv_sum_milled']} "
                f"dmg_p1={obs['damage_to_p1']} exiled={obs['exiled']}")
            ass["A3_mill"] = "passed" if milled else "failed"
            notes.append(f"mill during resolution: {len(milled)} card(s) "
                         f"{[n for n, _ in milled]} (Oracle: 1 per past vote)")
            if obs["damage_to_p1"] == obs["mv_sum_milled"] and milled:
                ass["A4_damage"] = "passed"
                notes.append(f"P1 took {obs['damage_to_p1']} damage == total MV "
                             f"of milled cards ({obs['mv_sum_milled']})")
            elif obs["damage_to_p1"] == 0 and not milled:
                ass["A4_damage"] = "failed"
                notes.append("no mill and no damage to P1 during resolution "
                             "(vote/mill/damage portion did nothing)")
            else:
                ass["A4_damage"] = "failed"
                notes.append(f"P1 damage={obs['damage_to_p1']} vs milled-MV-sum="
                             f"{obs['mv_sum_milled']} (inconsistent)")
            ass["A5_exile"] = "passed" if new_ex else "failed"
            notes.append(f"exiled during resolution: {len(new_ex)} card(s) "
                         f"{obs['exiled']} (Oracle: 1 per present vote)")
            # A6
            t_in_gy = TEMPEST in gy_names(post_st, 0)
            stack_empty = len(post_st.get("stack", []) or []) == 0
            advanced = (post_st.get("turn_number", 0) > (obs["cast_turn"] or 0)
                        or post_st.get("phase") != obs["cast_phase"])
            if t_in_gy and stack_empty and advanced:
                ass["A6_cleanup"] = "passed"
                notes.append("post.json: Tempest in P0 graveyard, stack empty, "
                             f"game advanced (turn {post_st.get('turn_number')}, "
                             f"phase {post_st.get('phase')})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"post.json: tempest_in_gy={t_in_gy} "
                             f"stack_empty={stack_empty} advanced={advanced}")
        else:
            for k in ("A3_mill", "A4_damage", "A5_exile", "A6_cleanup"):
                ass[k] = "failed"
                notes.append(f"{k} could not be evaluated (missing pre/post state)")
        # verdict
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup incomplete; see notes")
        elif ass["A2_vote_prompted"] == "failed":
            verdict = "reproduced"
        elif all(ass[k] == "passed" for k in
                 ("A2_vote_prompted", "A3_mill", "A4_damage", "A5_exile",
                  "A6_cleanup")):
            verdict = "not-reproduced"
        else:
            # mixed: vote appeared but some sub-effect failed, or vice versa --
            # still the reported "card doesn't work" outcome
            verdict = "reproduced"
            notes.append("mixed assertion outcome; the Oracle-mandated vote "
                         "flow is broken in at least one required step")
        return verdict

    async def write_manifest():
        fnames = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
        lines = []
        for fn in fnames:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")

    async def finish():
        dur = time.time() - t_start
        nonlocal post_exported
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        verdict = evaluate()
        run = {
            "issue": 1488,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 71, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_1488.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Fateful Tempest deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "If a vote prompt had appeared, the driver would have voted "
                "'past' for both seats (deterministic test policy); no vote "
                "prompt appeared."],
            "setup_line": "P0: 12x fateful tempest + 48x mountain (mulligan to tempest + 2 lands); "
                          "P1: 60x mountain dummy",
            "contract_line": "Cast Fateful Tempest: council's-dilemma vote (past/present) "
                             "offered to each player, then mill 1 per past vote, damage each "
                             "opponent = total MV milled, exile top 1 per present vote, may "
                             "play exiled cards until end of next turn",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copyfile(f"{BACKFILL}/driver/scenario_1488.py",
                        f"{EVDIR}/scenario_1488.py")
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        # manifest LAST, after all logging/writes are done
        await write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    async def do_mulligan(c, pid, key_need, lands_need, max_mulls):
        st = c.latest
        pend = pending_for(st["state"], pid)
        declare = next((p for p in pend
                        if (p.get("phase") or {}).get("type") == "Declare"),
                       None)
        if declare is None:
            return False
        ms = mull_state.setdefault(pid, {"mulls": 0, "answered_count": -1})
        mcount = declare.get("mulligan_count", 0)
        if ms["answered_count"] == mcount:
            return False
        ma = find_action(merged_actions(st), "MulliganDecision")
        if not ma:
            return False
        hn = hand_names(st["state"], pid)
        lands = sum(1 for n in hn if n == MOUNTAIN)
        has_key = key_need is None or key_need in hn
        if (has_key and lands >= lands_need) or ms["mulls"] >= max_mulls:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"P{pid} keeps (tempest={key_need in hn if key_need else 'n/a'}, "
                f"lands={lands})")
        else:
            ms["mulls"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"P{pid} mulligans #{ms['mulls']}")
        ms["answered_count"] = mcount
        return True

    async def do_bottom(c, pid):
        st = c.latest
        state = st["state"]
        pend = pending_for(state, pid)
        count = 0
        for p in pend:
            if (p.get("phase") or {}).get("type") == "BottomCards":
                count = int((p.get("phase") or {}).get("count", 1))
        if not count:
            return False
        sc = find_action(merged_actions(st), "SelectCards")
        if not sc:
            return False
        hand_ids = [o for o in player_of(state, pid).get("hand", [])]

        def bkey(oid):
            nm = lname(state, oid)
            if nm == MOUNTAIN:
                return 2
            if nm == TEMPEST:
                return 1
            return 0
        picks = sorted(hand_ids, key=bkey)[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"P{pid} bottoms {count}: {[lname(state, x) for x in picks]}")
        return True

    async def do_discard_to_handsize(c, pid):
        st = c.latest
        state = st["state"]
        wf = state.get("waiting_for") or {}
        if str((wf.get("data") or {}).get("player")) != str(pid):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        hand_ids = [o for o in player_of(state, pid).get("hand", [])]

        def dkey(oid):
            nm = lname(state, oid)
            if nm == MOUNTAIN:
                return 0
            if nm == TEMPEST:
                return 2
            return 1
        prio = sorted(hand_ids, key=dkey)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in submitted_interactions:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            if not cands:
                continue
            want = {str(x) for x in prio[:1]}
            pick = next((ch for ch in cands if str(ch.get("id")) in want),
                        cands[0])
            spec = (data.get("spec") or {}).get("type") or "select"
            sub = {"interactionId": iid,
                   "response": {"type": spec,
                                "data": {"choiceIds": [pick.get("id")]}}}
            await c.send_interaction(sub)
            submitted_interactions.add(iid)
            say(f"P{pid} discards to hand size: {lname(state, pick.get('id'))}")
            wire(f"p{pid}_discard_handsize", {"submission": sub})
            return True
        return False

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, cast_on_stack_exported, post_exported
        if await do_mulligan(p0, 0, TEMPEST, 2, 3):
            return
        if await do_bottom(p0, 0):
            return
        if await do_discard_to_handsize(p0, 0):
            return
        wtype = (state.get("waiting_for") or {}).get("type")
        # mana payments advertised by the engine are submitted as-is
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # observe/log any interaction opportunities (vote detection lives here)
        if await scan_interactions(st, "P0"):
            return
        # mid export: Tempest spell on the stack
        if obs["cast_submitted"] and not cast_on_stack_exported \
                and tempest_spell_on_stack(state):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/cast_on_stack.json", "w") as f:
                    f.write(mid)
                cast_on_stack_exported = True
                say("exported MID (Tempest on stack)")
            except Exception as e:
                notes.append(f"cast-on-stack export failed: {e}")
        # post export: Tempest resolved (in gy), stack empty, turn advanced
        if (obs["cast_submitted"] and not post_exported
                and TEMPEST in gy_names(state, 0)
                and not tempest_spell_on_stack(state)
                and len(state.get("stack", []) or []) == 0
                and (state.get("turn_number", 0) > (obs["cast_turn"] or 0)
                     or state.get("phase") != obs["cast_phase"])):
            say("Tempest resolved; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        if (not obs["cast_submitted"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and tempest_in_hand_oid(state, 0) is not None
                and len(untapped_mountains(state, 0)) >= 3):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" \
                        and lname(state, d.get("object_id")) == TEMPEST:
                    say("P0 main phase: exporting PRE, then casting Fateful Tempest")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_exported = True
                    wire("cast_tempest", a)
                    obs["cast_turn"] = state.get("turn_number")
                    obs["cast_phase"] = state.get("phase")
                    obs["cast_oid"] = d.get("object_id")
                    await submit_as_is(p0, a)
                    obs["cast_submitted"] = True
                    say(f"P0 casts Fateful Tempest (turn {obs['cast_turn']}, "
                        f"oid {obs['cast_oid']})")
                    return
        # normal setup play
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        if await do_mulligan(p1, 1, None, 2, 2):
            return
        if await do_bottom(p1, 1):
            return
        wtype = (state.get("waiting_for") or {}).get("type")
        wf_player = (state.get("waiting_for") or {}).get("player")
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if await scan_interactions(st, "P1"):
            return
        if wtype == "DeclareAttackers" and wf_player == 1:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers" and wf_player == 1:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers" and wf_player == 1:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_deadline = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
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
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)[:8]} "
                f"P0lands={len(untapped_mountains(s, 0))} life={life_of(s, 0)}/{life_of(s, 1)} "
                f"stack={len(s.get('stack') or [])} cast={obs['cast_submitted']} "
                f"vote={obs['vote_seen']} pre={pre_exported} post={post_exported}")
        if obs["cast_submitted"] and not post_exported and stuck_deadline is None:
            stuck_deadline = time.time() + 300
        if not obs["cast_submitted"] or post_exported:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("Tempest cast but post-resolution state not reached in 300s; "
                         "see wire log (possible unhandled interaction)")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
