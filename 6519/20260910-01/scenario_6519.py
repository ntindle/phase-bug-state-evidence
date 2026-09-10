#!/usr/bin/env python3
"""Issue #6519: Delver of Secrets - "Not transformed".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github, 2026-07-22): Delver of Secrets on the battlefield; during
upkeep the top card of the library is revealed; it is a sorcery; Delver does
NOT transform. Recurrence of previously-fixed behavior (#2367).

Oracle text (pinned card-data.json, 'delver of secrets'):
  "At the beginning of your upkeep, look at the top card of your library.
   You may reveal that card. If an instant or sorcery card is revealed this
   way, transform ~."
Parsed (card-data.json 'triggers'): one Phase/Upkeep trigger, constraint
OnlyDuringYourTurn, trigger_zones=[Battlefield]:
  Dig (look at top 1, reveal=false) -> optional Reveal (ParentTarget) ->
  Transform SelfRef, condition RevealedHasCardType[Instant, Sorcery].

Setup (native engine, two human-client seats):
  P0: 12x delver of secrets + 24x island + 24x divination (sorcery).
      (12x density: engine accepts >4-of for custom games; mulligan to
      delver + 2 islands.)
  P1: 60x island dummy (plays a land, passes; never attacks).

Expected (per card text):
  E1: Delver on P0's battlefield at the start of P0's upkeep.
  E2: the upkeep trigger is put on the stack (TriggeredAbility, Dig effect,
      source = Delver).
  E3: during resolution the controller is offered the optional reveal
      (decideOptionalEffect); driver accepts.
  E4: if the revealed card is an instant or sorcery, Delver transforms into
      Insectile Aberration (3/2).
  E5: stack empties and the game proceeds (Draw / PreCombatMain).

The top card at each upkeep is random, so the driver loops over successive
P0 upkeeps (up to 10), accepting the reveal each time, until an upkeep where
the revealed card is an instant or sorcery. That upkeep is the decisive one:
pre.json = start of that upkeep (trigger on stack, Delver on BF),
post.json = after the trigger resolved.

Assertions:
  A1_setup_ok        decisive pre.json: P0 upkeep, Delver on BF, life 20
  A2_reveal_offered  the decideOptionalEffect prompt appeared and the driver
                     accepted; the revealed card is recorded in state
  A3_sorcery_revealed the revealed card is an instant or sorcery
                     (card-data lookup; name recorded)
  A4_transformed     the Delver object transformed into Insectile Aberration
                     (3/2) in post.json
  A5_cleanup         post.json: stack empty, Delver-object still on BF,
                     game proceeding (Draw/PreCombatMain, Priority)

Verdict rule: reproduced iff A1, A2, A3 passed and A4 failed (the reported
"outcome": sorcery revealed, no transform). not-reproduced iff A1..A5 pass.
blocked iff no decisive upkeep (instant/sorcery reveal) was reached.

Evidence: evidence/6519/<run-id>/pre.json (decisive upkeep start),
post.json (after trigger resolution), run.json, manifest.sha256, summary.png,
scenario_6519.py, wire_log.jsonl, scenario_run.log
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
RUN_ID = os.environ.get("RUN_ID", "20260910-01")
ISSUE = 6519
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DELVER = "delver of secrets"
ABERRATION = "insectile aberration"
ISLAND = "island"
SORC = "divination"  # sorcery filler

P0_DECK = [(DELVER, 12), (ISLAND, 24), (SORC, 24)]
P1_DECK = [(ISLAND, 60)]
MAX_UPKEEPS = 10

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.78.0/data/card-data.json"))

SERVER_IDENTITY = {
    "version": "v0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
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


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def pt(obj):
    return num(obj.get("power")), num(obj.get("toughness"))


def delver_ids(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == DELVER]


def hand_lnames(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [lname(state, o) for o in p.get("hand", [])]
    return []


def hand_ids(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def untapped_islands(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == ISLAND and not o.get("tapped")]


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


def delver_trigger_stack_ids(state):
    """Stack ids of Delver upkeep triggers (TriggeredAbility, Dig, source Delver)."""
    out = []
    for e in state.get("stack", []) or []:
        kind = e.get("kind", {}) or {}
        if kind.get("type") != "TriggeredAbility":
            continue
        src = e.get("source_id")
        if src is not None and lname(state, src) == DELVER:
            out.append(e.get("id"))
    return out


def journal_reveal_oids(state):
    """Object ids revealed this game, from resolved_rules_journal.

    The engine records each reveal as an `Information` command with
    edit == "Reveal" (audience Public, lifetime UntilZoneChange); the
    `revealed_cards` top-level list is not populated for this flow, so the
    journal is the authoritative record of what was revealed and when.
    """
    out = []
    j = state.get("resolved_rules_journal", {}) or {}
    for e in j.get("entries", []) or []:
        cmd = e.get("command", {}) or {}
        info = cmd.get("Information")
        if isinstance(info, dict) and info.get("edit") == "Reveal":
            for occ in info.get("occurrences", []) or []:
                oid = occ.get("object_id")
                if oid is not None and int(oid) not in out:
                    out.append(int(oid))
    return out


def revealed_names(state):
    """Best-effort list of revealed card names from state['revealed_cards']."""
    out = []
    for entry in state.get("revealed_cards", []) or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            for k in ("name", "base_name", "card_name", "face_name"):
                if entry.get(k):
                    out.append(str(entry[k]))
                    break
            else:
                out.append(json.dumps(entry, default=str)[:120])
        else:
            out.append(str(entry))
    return out


def card_types(name):
    c = CARD_DATA.get(str(name).lower())
    if not c:
        return None
    ct = c.get("card_type") or {}
    return [str(t) for t in (ct.get("core_types") or [])]


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_reveal_offered", "A3_sorcery_revealed",
            "A4_transformed", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}
    delver_cast = False
    submitted_interactions = set()

    # per-upkeep tracking
    upkeep_n = 0
    cur = None  # dict for the upkeep currently being observed
    decisive = None  # filled when an instant/sorcery is revealed

    def new_upkeep(turn):
        return {"n": turn, "pre_reveal_oids": [], "trigger_ids": [],
                "reveal_accepted": False, "reveal_iid": None,
                "resolved": False, "revealed_card": None,
                "revealed_types": None, "delver_oid": None}

    async def finish():
        dur = time.time() - t_start
        # evaluate from saved states
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")
        if pre_st is not None and post_st is not None and decisive:
            d = decisive
            dids = delver_ids(pre_st, 0)
            if (len(dids) >= 1 and pre_st.get("active_player") == 0
                    and pre_st.get("phase") == "Upkeep"
                    and life_of(pre_st, 0) == 20):
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre.json: P0 upkeep, Delver oid(s) {dids} on BF, life 20")
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append("pre.json missing Delver-on-BF P0 upkeep setup")
            if d["reveal_accepted"] and d["revealed_card"]:
                ass["A2_reveal_offered"] = "passed"
                notes.append(f"decideOptionalEffect accepted (iid {d['reveal_iid']}); "
                             f"revealed: {d['revealed_card']}")
            else:
                ass["A2_reveal_offered"] = "failed"
                notes.append("reveal was not offered/accepted or card not recorded")
            types = d["revealed_types"] or []
            if any(t in ("Instant", "Sorcery") for t in types):
                ass["A3_sorcery_revealed"] = "passed"
                notes.append(f"revealed card {d['revealed_card']} has types {types} "
                             f"(instant/sorcery precondition met)")
            else:
                ass["A3_sorcery_revealed"] = "failed"
                notes.append(f"revealed card {d['revealed_card']} types={types} "
                             f"(not instant/sorcery)")
            # A4: the Delver object in post.json
            o = get_obj(post_st, d["delver_oid"]) if d["delver_oid"] else {}
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            p, t = pt(o)
            transformed = (o.get("transformed") is True) or ("insectile" in nm)
            if transformed and p == 3 and t == 2:
                ass["A4_transformed"] = "passed"
                notes.append(f"Delver oid {d['delver_oid']} transformed: {nm} {p}/{t}")
            else:
                ass["A4_transformed"] = "failed"
                notes.append(f"Delver oid {d['delver_oid']} NOT transformed in post.json: "
                             f"name={nm} {p}/{t} transformed_flag={o.get('transformed')} "
                             f"(REPORTED BUG)")
            slen = len(post_st.get("stack", []) or [])
            ph = post_st.get("phase")
            wf = (post_st.get("waiting_for") or {}).get("type")
            zone = o.get("zone")
            if slen == 0 and zone == "Battlefield" and wf in (
                    "Priority", "DeclareAttackers", None):
                ass["A5_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, Delver-object on BF, "
                             f"game proceeding (phase={ph}, wf={wf})")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"post.json cleanup wrong: stack={slen}, zone={zone}, "
                             f"phase={ph}, wf={wf}")
        else:
            notes.append("no decisive upkeep captured; cannot evaluate A1..A5")
            for k in ass:
                if ass[k] == "not-run":
                    ass[k] = "failed"
        core = ["A1_setup_ok", "A2_reveal_offered", "A3_sorcery_revealed",
                "A4_transformed"]
        if all(ass[k] == "passed" for k in
               ("A1_setup_ok", "A2_reveal_offered", "A3_sorcery_revealed",
                "A4_transformed", "A5_cleanup")):
            verdict = "not-reproduced"
        elif (ass["A1_setup_ok"] == "passed" and ass["A2_reveal_offered"] == "passed"
              and ass["A3_sorcery_revealed"] == "passed"
              and ass["A4_transformed"] == "failed"):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive: setup or decisive upkeep incomplete; see notes")
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "game_code": p0.game_code,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6519.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "upkeeps_observed": upkeep_n,
            "decisive_upkeep": (decisive["n"] if decisive else None),
            "revealed_card": (decisive["revealed_card"] if decisive else None),
            "revealed_types": (decisive["revealed_types"] if decisive else None),
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Delver of Secrets + 24x Divination deck density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The top card at each upkeep is engine-shuffled; the driver loops "
                "upkeeps until an instant/sorcery is revealed (max 10 upkeeps).",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x delver of secrets + 24x island + 24x divination "
                          "(mulligan to delver + 2 islands); P1: 60x island dummy",
            "contract_line": "At the start of P0's upkeep with Delver on the battlefield, "
                             "reveal the top card; if it is an instant or sorcery, "
                             "Delver transforms into Insectile Aberration",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": ass, "notes": notes,
                       "verdict": verdict}, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    async def handle_interactions(c, st):
        """Accept the Delver optional-reveal prompt (decideOptionalEffect)."""
        nonlocal cur
        vi = st.get("viewer_interaction") or {}
        if not (vi.get("canSubmit") and (vi.get("opportunities") or [])):
            return False
        acted = False
        for opp in vi["opportunities"]:
            iid = opp.get("interactionId")
            if iid in submitted_interactions:
                continue
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "exactChoices":
                continue
            data = resp.get("data", {}) or {}
            pick = None
            is_reveal_prompt = False
            for ch in data.get("choices", []) or []:
                if ch.get("status", {}).get("type") != "available":
                    continue
                surfaces = ch.get("surfaces", []) or []
                sdata = [s.get("data", {}) or {} for s in surfaces]
                if any(sd.get("code") == "decideOptionalEffect" for sd in sdata):
                    is_reveal_prompt = True
                if any(s.get("type") == "value" and sd.get("role") == "accept"
                       and (sd.get("value") is True
                            or str(sd.get("value")).lower() == "true")
                       for s, sd in zip(surfaces, sdata)):
                    pick = ch
            if is_reveal_prompt and pick is not None:
                sub = {"interactionId": iid, "response":
                       {"type": "choose", "data": {"choiceId": pick["id"]}}}
                say(f"accepting optional reveal: {iid} choice {pick['id']}")
                wire("reveal_accepted", {"interactionId": iid,
                                         "choice": pick["id"],
                                         "opportunity": opp})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                if cur is not None:
                    cur["reveal_accepted"] = True
                    cur["reveal_iid"] = iid
                acted = True
                await asyncio.sleep(0.5)
            elif iid not in submitted_interactions:
                # log unmatched prompt shapes once (diagnostic)
                wire("unmatched_opportunity", {"who": c.name,
                                              "interactionId": iid,
                                              "opportunity": opp})
                submitted_interactions.add(iid)
        return acted

    async def p0_tick(st, acts, state):
        nonlocal upkeep_n, cur, decisive, delver_cast
        # 1. interaction prompts first
        if await handle_interactions(p0, st):
            return
        wtype = (state.get("waiting_for") or {}).get("type")
        # log waiting_for transitions (diagnostic for trigger flow)
        wfkey = (state.get("turn_number"), state.get("phase"), wtype,
                 state.get("priority_player"))
        if wfkey != kept.get("P0_wfkey"):
            kept["P0_wfkey"] = wfkey
            wire("wf_transition", {"turn": state.get("turn_number"),
                                   "phase": state.get("phase"), "wf": wtype,
                                   "pp": state.get("priority_player"),
                                   "stack": len(state.get("stack", []) or []),
                                   "delver": delver_ids(state, 0)})
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_lnames(state, 0)
            isl = sum(1 for n in hn if n == ISLAND)
            mulls = kept.get("P0_m", 0)
            if (DELVER in hn and isl >= 2) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (delver={DELVER in hn}, islands={isl})")
            else:
                kept["P0_m"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1}")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                kept["P0_bottomed"] = True
                hand = hand_ids(state, 0)
                # bottom non-delver, non-island first (keep delvers+islands)
                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm not in (DELVER, ISLAND) else 1
                picks = sorted(hand, key=bkey)[:1]
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": picks}})
                say(f"P0 bottoms {[lname(state, x) for x in picks]}")
            return
        # 2. upkeep detection: P0 upkeep with Delver on BF
        if (state.get("active_player") == 0 and state.get("phase") == "Upkeep"
                and delver_ids(state, 0) and cur is None
                and decisive is None and upkeep_n < MAX_UPKEEPS):
            upkeep_n += 1
            cur = new_upkeep(upkeep_n)
            cur["delver_oid"] = delver_ids(state, 0)[0]
            say(f"--- P0 upkeep #{upkeep_n}: Delver oid {cur['delver_oid']} on BF; "
                f"exporting upkeep pre")
            pre = await p0.export_state()
            with open(f"{EVDIR}/upkeep_{upkeep_n}_pre.json", "w") as f:
                f.write(pre)
            cur["pre_reveal_oids"] = journal_reveal_oids(json.loads(pre)["state"])
            cur["trigger_ids"] = delver_trigger_stack_ids(state)
            say(f"upkeep #{upkeep_n}: trigger stack ids {cur['trigger_ids']}, "
                f"reveals so far {cur['pre_reveal_oids']}")
        # 3. resolution detection for the upkeep under observation
        if cur is not None and not cur["resolved"]:
            tids = delver_trigger_stack_ids(state)
            if (cur["reveal_accepted"] and not tids
                    and wtype != "OptionalEffectChoice"):
                cur["resolved"] = True
                say(f"upkeep #{cur['n']}: trigger resolved; exporting upkeep post")
                post = await p0.export_state()
                with open(f"{EVDIR}/upkeep_{cur['n']}_post.json", "w") as f:
                    f.write(post)
                post_st = json.loads(post)["state"]
                post_oids = journal_reveal_oids(post_st)
                new_oids = [o for o in post_oids
                            if o not in cur["pre_reveal_oids"]]
                new_revealed = [str(obj_name(post_st, o)) for o in new_oids]
                say(f"upkeep #{cur['n']}: newly revealed oids {new_oids} "
                    f"-> {new_revealed}")
                wire("upkeep_resolved", {"n": cur["n"],
                                         "newly_revealed_oids": new_oids,
                                         "newly_revealed": new_revealed})
                if new_revealed:
                    cur["revealed_card"] = new_revealed[-1]
                    cur["revealed_types"] = card_types(cur["revealed_card"])
                    say(f"upkeep #{cur['n']}: revealed {cur['revealed_card']} "
                        f"types={cur['revealed_types']}")
                    if any(t in ("Instant", "Sorcery")
                           for t in (cur["revealed_types"] or [])):
                        decisive = cur
                        shutil.copy(f"{EVDIR}/upkeep_{cur['n']}_pre.json",
                                    f"{EVDIR}/pre.json")
                        shutil.copy(f"{EVDIR}/upkeep_{cur['n']}_post.json",
                                    f"{EVDIR}/post.json")
                        say(f"DECISIVE upkeep #{cur['n']}: "
                            f"{cur['revealed_card']} revealed; finishing")
                        await finish()
                        return "done"
                else:
                    notes.append(f"upkeep #{cur['n']}: no new revealed card recorded")
                cur = None
        # 4. priority play
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
                say("P0 declares no attackers")
            return
        if wtype == "DeclareBlockers":
            db = find_action(acts, "DeclareBlockers")
            if db:
                d = dict(db.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
                say("P0 declares no blockers")
            return
        if wtype == "DiscardToHandSize":
            da = find_action(acts, "Discard")
            if da:
                # discard islands first, then divinations; keep delvers
                hand = hand_ids(state, 0)
                def dkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm == ISLAND else (1 if nm == SORC else 2)
                picks = sorted(hand, key=dkey)[:1]
                say(f"P0 discards {[lname(state, x) for x in picks]}")
                d = dict(da.get("data", {})); d["cards"] = picks
                await p0.send_action({"type": "Discard", "data": d})
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        if (not delver_cast and not delver_ids(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and DELVER in hand_lnames(state, 0)
                and len(untapped_islands(state, 0)) >= 2):
            for a in acts:
                dd = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, dd.get("object_id")) == DELVER:
                    say("P0 casts delver of secrets")
                    wire("cast_delver", a)
                    await submit_as_is(p0, a)
                    delver_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(p0, pp)

    async def p1_tick(st, acts, state):
        if await handle_interactions(p1, st):
            return
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            db = find_action(acts, "DeclareBlockers")
            if db:
                d = dict(db.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        pl = find_action(acts, "PlayLand")
        if pl and state.get("phase") in ("PreCombatMain", "PostCombatMain"):
            await submit_as_is(p1, pl)
            return
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(p1, pp)

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    done = False
    while time.time() - t0 < 1500 and not done:
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
                r = await tick(st, merged_actions(st), st["state"])
                if r == "done":
                    done = True
                    break
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if done:
            break
        if decisive is not None:
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)[:8]} "
                f"delver={delver_ids(s, 0)} stack={len(s.get('stack') or [])} "
                f"upkeeps={upkeep_n} cast={delver_cast}")
    if not done and decisive is None:
        notes.append(f"global timeout or upkeep budget exhausted "
                     f"(upkeeps observed: {upkeep_n}) before a decisive "
                     f"instant/sorcery reveal")
        await finish()


asyncio.run(main())
