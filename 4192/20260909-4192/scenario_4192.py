#!/usr/bin/env python3
"""Issue #4192: Stuck decision: ReplacementChoice (Kaalia of the Vast).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.3.0 f4053d0, 2026-06-23):
  Diagnostic: Waiting for: ReplacementChoice | Stuck players: 0
  "What happened" was left blank. Reporter comment (vandsmith, 2026-06-23):
  "i was playing a game and trigger on attack (put an angel, demon, from hand
   to battlefield tapped and attacking.  then game crashed."
  mike-theDude (2026-07-26): the attack wording describes Kaalia of the Vast,
  and PR #4306 (6bf164900) fixes the exact zero-action ReplacementChoice
  diagnostic (an empty or already-consumed pending replacement re-parking the
  game on an invisible ReplacementChoice); regressions cover both absent and
  empty pending records. Asked to close/delete as stale if it no longer
  reproduces.

Oracle text (pinned card-data.json v0.78.0):
  Kaalia of the Vast: "Flying / Whenever Kaalia attacks an opponent, you may
    put an Angel, Demon, or Dragon creature card from your hand onto the
    battlefield tapped and attacking that opponent."
  Serra Angel: Angel, 4/4, Flying, Vigilance (no replacement effects).

Expected behavior: Kaalia attacks, her trigger offers the may-choice, the
controller answers yes, selects the Angel, the Angel enters the battlefield
tapped and attacking, and the game proceeds. No ReplacementChoice stall with
stuck players 0.

The reported failure: after the attack trigger, the game parks on
ReplacementChoice with no actionable submission (stuck players: 0) - the
zero-action replacement-softlock signature.

Setup: P0 (8x Kaalia / 8x Serra Angel / 12x Mountain / 12x Plains / 12x Swamp).
P0 mulligans to Kaalia+Angel+3 lands, casts Kaalia when {1}{R}{W}{B} mana is
available, attacks with her, answers the trigger, puts Serra Angel in tapped
and attacking. P1 is a do-nothing 60x Forest deck (no blockers).

Assertions:
  A1 setup_ok ......... Kaalia cast and attacking; pre.json exported at
                        DeclareAttackers (Kaalia BF, Angel in hand).
  A2 trigger_offered .. Kaalia's attack trigger observed on the stack, or the
                        may-choice opportunity was offered to P0.
  A3 no_stall ......... no ReplacementChoice wait without an actionable
                        submission persisted >60s (watchdog never fired).
  A4 angel_enters ..... post.json: Serra Angel on P0 battlefield, tapped, with
                        attacking evidence (attacking flag or combat damage).
  A5 cleanup .......... post.json: stack empty, game proceeding.

Verdict rule:
  reproduced .... the reported stall signature: a ReplacementChoice wait with
                  no actionable submission persisting >60s (stall_observed), or
                  the trigger/put-in step answered but the Angel never enters
                  within 150s (entry_stall, related failure).
  not-reproduced  the trigger completes per Oracle and the game proceeds
                  (A3+A4+A5 pass).
  blocked ....... the setup gate never opens (Kaalia never cast/attacked, no
                  trigger ever seen); no trustworthy engine behavior observed.

Evidence: evidence/4192/<run-id>/pre.json, post.json, mid_angel_entered.json,
run.json, manifest.sha256, summary.png, scenario_4192.py, wire_log.jsonl,
scenario_run.log (+ mid_stall.json only if the stall fires).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-4192"
EVDIR = f"{BACKFILL}/evidence/4192/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KAALIA = "Kaalia of the Vast"
ANGEL = "Serra Angel"
MOUNTAIN = "Mountain"
PLAINS = "Plains"
SWAMP = "Swamp"
FOREST = "Forest"

P0_DECK = [(KAALIA, 8), (ANGEL, 8), (MOUNTAIN, 12), (PLAINS, 12), (SWAMP, 12)]
P1_DECK = [(FOREST, 60)]

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
    "observed_at": "2026-09-09",
    "source": "ServerHello on 127.0.0.1:9374 matched the pinned verified release; "
              "fresh isolated server for this run",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def obj_name(state, oid):
    o = get_obj(state, oid)
    return o.get("base_name") or o.get("name") or "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, name=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (name is None or lname(state, oid) == name.lower())]


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_offered", "A3_no_stall",
            "A4_angel_enters", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    pre_exported = False
    post_exported = False
    kaalia_cast = False
    settle_empty = 0

    obs = {
        "kaalia_attack_declared": False,
        "kaalia_trigger_seen": False,
        "trigger_seen_at": None,
        "may_answered": None,          # 'yes' once the may-choice is answered
        "may_answered_at": None,
        "angel_selected": False,
        "angel_selected_at": None,
        "angel_entered_at": None,
        "life_pre": None,
        "stall_observed": False,
        "entry_stall": False,
        "replacement_waits": [],
        "rejections": [],
        "may_choices_seen": 0,
    }
    shapes_logged = set()
    submitted_iids = set()
    repl_wait_start = None

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def kaalia_on_bf(state):
        ids = [o for o in bf_ids(state, 0) if lname(state, o) == KAALIA.lower()]
        return ids[0] if ids else None

    def angel_on_bf(state):
        ids = [o for o in bf_ids(state, 0) if lname(state, o) == ANGEL.lower()]
        return ids[0] if ids else None

    def angel_in_hand(state):
        return any(lname(state, o) == ANGEL.lower()
                   for o in hand_ids(state, 0))

    def kaalia_in_hand(state):
        return any(lname(state, o) == KAALIA.lower()
                   for o in hand_ids(state, 0))

    def stack_signatures(state):
        out = []
        for e in state.get("stack", []) or []:
            src = e.get("source") or e.get("source_id")
            try:
                src_name = lname(state, src) if src is not None else None
            except Exception:
                src_name = None
            out.append({"id": e.get("id"), "source_name": src_name,
                        "keys": sorted(e.keys())})
        return out

    def drain_errors(c, tag):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                rec = {"who": tag, "type": t,
                       "data": json.dumps(data, default=str)[:600]}
                obs["rejections"].append(rec)
                say(f"[{tag}] REJECTION {t}: {rec['data'][:200]}")
                wire("rejection", rec)

    async def scan_interactions(st, c, tag, pid, state):
        """Handle Kaalia's 'you may put an Angel/Demon/Dragon' may-choice and
        the card-selection prompt. Returns True if an interaction submitted."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            low = (blob + " " + json.dumps(opp, default=str)).lower()
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            key = (tag, rtype, spec_type, blob[:100])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{tag}] interaction rtype={rtype} spec={spec_type} "
                    f"n_choices={len(chs)} choices=[{blob[:200]}]")
                wire("interaction_shape", {"who": tag, "rtype": rtype,
                                           "spec_type": spec_type,
                                           "interaction": opp})
            if iid in submitted_iids:
                continue
            # --- Kaalia's may-choice: exactChoices yes/no. The engine advertises
            # this as OptionalEffectChoice/decideOptionalEffect with boolean
            # value choices ("true" = accept, "false" = decline). Only handled
            # after her trigger was seen; priority menus contain a pass choice
            # and are never touched. ---
            if (rtype == "exactChoices" and obs["kaalia_trigger_seen"]
                    and obs["may_answered"] is None
                    and len(chs) <= 4
                    and not any("pass" in t.lower() for t in texts)):
                yes = next((ch for ch in chs
                            if re.match(r"^(yes|put\b|true\b)", choice_text(ch),
                                         re.IGNORECASE)), None)
                no = next((ch for ch in chs
                           if re.match(r"^(no\b|false\b)", choice_text(ch),
                                        re.IGNORECASE)), None)
                if yes is not None or no is not None:
                    obs["may_choices_seen"] += 1
                    wire("may_choice_offered",
                         {"who": tag, "choices": texts})
                if yes is not None:
                    sub = {"interactionId": iid, "response":
                           {"type": "choose",
                            "data": {"choiceId": yes.get("id")}}}
                    say(f"[{tag}] Kaalia may-choice: answering YES "
                        f"('{choice_text(yes)}')")
                    wire("may_choice_submission",
                         {"who": tag, "submission": sub})
                    obs["may_answered"] = "yes"
                    obs["may_answered_at"] = time.time()
                    await c.send_interaction(sub)
                    submitted_iids.add(iid)
                    acted = True
                    continue
                if no is not None:
                    say(f"[{tag}] may-choice offered but no YES choice present: "
                        f"{texts} - leaving unanswered")
                    wire("may_choice_no_yes", {"who": tag, "choices": texts})
            # --- card selection (schema only; pick Serra Angel) ---
            if rtype == "schema":
                angel_ch = next((ch for ch in chs
                                 if "serra angel" in choice_text(ch).lower()),
                                None)
                if angel_ch is not None:
                    cid = angel_ch.get("id")
                    sub = {"interactionId": iid, "response":
                           {"type": spec_type or "sequence",
                            "data": {"choiceIds": [cid]}}}
                    say(f"[{tag}] Kaalia put-choice: selecting Serra Angel")
                    wire("put_choice_submission",
                         {"who": tag, "submission": sub,
                          "candidate": {k: angel_ch.get(k)
                                        for k in ("id", "text", "label", "name")}})
                    obs["angel_selected"] = True
                    obs["angel_selected_at"] = time.time()
                    await c.send_interaction(sub)
                    submitted_iids.add(iid)
                    acted = True
                    continue
                wire("unhandled_schema_prompt",
                     {"who": tag, "choices": texts[:10]})
        return acted

    async def mulligan_tick(c, pid, acts, state, tag):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_names(state, pid)
            if tag == "P0":
                has_k = KAALIA.lower() in hn
                has_a = ANGEL.lower() in hn
                lands = sum(1 for n in hn
                            if n in (MOUNTAIN.lower(), PLAINS.lower(),
                                      SWAMP.lower()))
                mulls = kept.get(f"{tag}_mulls", 0)
                if (has_k and has_a and lands >= 3) or mulls >= 3:
                    kept[tag] = True
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Keep"}}})
                    say(f"{tag} keeps (kaalia={has_k}, angel={has_a}, lands={lands})")
                else:
                    kept[f"{tag}_mulls"] = mulls + 1
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Mulligan"}}})
                    say(f"{tag} mulligans #{mulls + 1}")
            else:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps 7")
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{tag}_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, pid)
                if tag == "P0":
                    seen = {}
                    def bottom_key(oid):
                        nm = lname(state, oid)
                        if nm in (MOUNTAIN.lower(), PLAINS.lower(), SWAMP.lower()):
                            k = seen.get("land", 0)
                            seen["land"] = k + 1
                            return (0 if k >= 3 else 4, nm)
                        for key, want in ((KAALIA.lower(), 1), (ANGEL.lower(), 1)):
                            if nm == key:
                                kk = seen.get(key, 0)
                                seen[key] = kk + 1
                                return (1 if kk >= want else 4, nm)
                        return (2, nm)
                    picks = sorted(hids, key=bottom_key)[:count]
                else:
                    picks = hids[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def generic_decision(c, pid, tag, acts, state, wtype):
        """Non-priority waiting_for handling shared by both seats."""
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
                say(f"{tag} submits advertised OrderTriggers")
                return True
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                dd = sub.setdefault("data", {})
                for k in ("attacks", "attackers", "blocks", "blockers",
                          "assignments"):
                    if k in dd:
                        dd[k] = [] if isinstance(dd[k], list) else {}
                await c.send_action({"type": wtype, "data": dd})
                say(f"{tag} declares empty {wtype}")
                return True
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say(f"{tag} submits advertised AssignCombatDamage")
                return True
        return False

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, kaalia_cast
        nonlocal repl_wait_start, settle_empty
        drain_errors(p0, "P0")
        wtype = (state.get("waiting_for") or {}).get("type") or ""
        if await mulligan_tick(p0, 0, acts, state, "P0"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # --- Kaalia attack-trigger detection on the stack. Gated on an actual
        # attack declaration: the spell itself on the stack also names Kaalia
        # as its source, so an ungated check fires spuriously on the cast. ---
        if not obs["kaalia_trigger_seen"] and obs["kaalia_attack_declared"]:
            for sig in stack_signatures(state):
                if sig["source_name"] and "kaalia" in sig["source_name"]:
                    obs["kaalia_trigger_seen"] = True
                    obs["trigger_seen_at"] = time.time()
                    say("Kaalia attack trigger observed on stack")
                    wire("kaalia_trigger", {"signatures": stack_signatures(state)})
                    break
        # --- ReplacementChoice stall tracking (the reported signature) ---
        if "ReplacementChoice" in wtype:
            vi = get_vi(st)
            actionable = vi is not None or any(
                a["type"] not in ("PassPriority",) for a in acts)
            rec = {"turn": state.get("turn_number"), "phase": state.get("phase"),
                   "revision": p0.revision, "actionable": actionable,
                   "n_legal": len(acts),
                   "vi_can_submit": vi is not None}
            if not obs["replacement_waits"] or \
                    obs["replacement_waits"][-1]["revision"] != rec["revision"]:
                obs["replacement_waits"].append(rec)
                say(f"P0 ReplacementChoice wait rev={rec['revision']} "
                    f"actionable={actionable} n_legal={len(acts)}")
                wire("replacement_wait", rec)
            if repl_wait_start is None:
                repl_wait_start = time.time()
            if not actionable and time.time() - repl_wait_start > 60:
                obs["stall_observed"] = True
                say("STALL: ReplacementChoice >60s with no actionable submission")
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_stall.json", "w") as f:
                        f.write(mid)
                except Exception as e:
                    notes.append(f"mid_stall export failed: {e}")
                return
        else:
            repl_wait_start = None
        # --- interactions (may-choice + put-choice) ---
        if await scan_interactions(st, p0, "P0", 0, state):
            return
        # --- angel-entered checkpoint ---
        if (obs["angel_entered_at"] is None
                and angel_on_bf(state) is not None):
            obs["angel_entered_at"] = time.time()
            say(f"Serra Angel entered the battlefield (tapped="
                f"{get_obj(state, angel_on_bf(state)).get('tapped')})")
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid_angel_entered.json", "w") as f:
                    f.write(mid)
                wire("angel_entered_snapshot",
                     {k: get_obj(json.loads(mid)["state"],
                                 angel_on_bf(json.loads(mid)["state"])).get(k)
                      for k in ("zone", "tapped", "controller", "owner",
                                "base_name", "attacking", "is_attacking")})
            except Exception as e:
                notes.append(f"mid_angel_entered export failed: {e}")
        # --- post checkpoint: angel on BF, stack settled ---
        if (not post_exported and obs["angel_entered_at"] is not None):
            if len(state.get("stack", []) or []) == 0:
                settle_empty += 1
            else:
                settle_empty = 0
            if settle_empty >= 3:
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("POST exported (Angel on battlefield, stack settled)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                return
        # --- DeclareAttackers: attack with Kaalia. Placed BEFORE generic_decision,
        # which would otherwise declare empty attackers and consume the wait. ---
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            koid = kaalia_on_bf(state)
            ready = ([koid] if (koid is not None
                                and not get_obj(state, koid).get("tapped"))
                     else [])
            if ready and not pre_exported:
                try:
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_st = json.loads(pre)["state"]
                    obs["life_pre"] = life_of(pre_st, 1)
                    pre_exported = True
                    say(f"PRE exported: Kaalia BF ready, Angel in hand="
                        f"{angel_in_hand(pre_st)}, P1 life={obs['life_pre']}")
                    wire("pre_export",
                         {"kaalia_bf": koid is not None,
                          "angel_in_hand": angel_in_hand(pre_st),
                          "life_pre": obs["life_pre"]})
                except Exception as e:
                    notes.append(f"pre export failed: {e}")
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = [
                    [o, {"type": "Player", "data": 1}] for o in ready]
                sub["data"]["bands"] = []
                wire("declare_attackers_submit", sub["data"])
                await p0.send_action({"type": "DeclareAttackers",
                                      "data": sub["data"]})
                if ready:
                    obs["kaalia_attack_declared"] = True
                    say(f"P0 attacks with Kaalia (oid {ready[0]})")
                else:
                    say("P0 declares empty attackers (Kaalia not ready)")
            else:
                say("P0 DeclareAttackers WAIT: no action offered")
            return
        if await generic_decision(p0, 0, "P0", acts, state, wtype):
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 1:
            # P1 never blocks: handled by its own tick; defensive no-op
            pass
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        phase = state.get("phase")
        hn = hand_names(state, 0)
        # cast Kaalia once {1}{R}{W}{B} mana is ready
        if (not kaalia_cast and kaalia_in_hand(state)
                and phase in ("PreCombatMain", "PostCombatMain")):
            unt = [o for o in bf_ids(state, 0)
                   if not get_obj(state, o).get("tapped")]
            has_m = any(lname(state, o) == MOUNTAIN.lower() for o in unt)
            has_p = any(lname(state, o) == PLAINS.lower() for o in unt)
            has_s = any(lname(state, o) == SWAMP.lower() for o in unt)
            if has_m and has_p and has_s and len(unt) >= 4:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and \
                            lname(state, d.get("object_id")) == KAALIA.lower():
                        say("P0 casts Kaalia of the Vast")
                        wire("cast_kaalia", a)
                        await submit_as_is(p0, a)
                        kaalia_cast = True
                        return
            elif time.time() - obs.get("last_mana_dbg", 0) > 30:
                obs["last_mana_dbg"] = time.time()
                say(f"[P0] Kaalia in hand, waiting on mana: untapped={len(unt)} "
                    f"M={has_m} P={has_p} S={has_s}")
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        # fallback: pass via viewer_interaction exactChoices menu
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                resp = opp.get("response", {}) or {}
                if resp.get("type") != "exactChoices":
                    continue
                chs = resp.get("data", {}).get("choices", []) or []
                pc = next((ch for ch in chs
                           if "pass" in choice_text(ch).lower()), None)
                if pc is not None and opp.get("interactionId") not in submitted_iids:
                    sub = {"interactionId": opp["interactionId"], "response":
                           {"type": "choose",
                            "data": {"choiceId": pc.get("id")}}}
                    say("P0 passes priority via viewer_interaction fallback")
                    await p0.send_interaction(sub)
                    submitted_iids.add(opp["interactionId"])
                    return

    async def p1_tick(st, acts, state):
        drain_errors(p1, "P1")
        wtype = (state.get("waiting_for") or {}).get("type") or ""
        if await mulligan_tick(p1, 1, acts, state, "P1"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        # P1 never answers trigger choices (not its trigger); log only
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                resp = opp.get("response", {}) or {}
                rtype = resp.get("type")
                data = resp.get("data", {}) or {}
                chs = data.get("choices") or data.get("candidates") or []
                key = ("P1", rtype, " // ".join(choice_text(c) for c in chs)[:80])
                if key not in shapes_logged:
                    shapes_logged.add(key)
                    wire("p1_interaction_shape",
                         {"rtype": rtype, "choices":
                          [choice_text(c) for c in chs][:10]})
        if await generic_decision(p1, 1, "P1", acts, state, wtype):
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

    def angel_snapshot(state, oid):
        o = get_obj(state, oid)
        return {k: o.get(k) for k in
                ("zone", "tapped", "controller", "owner", "base_name", "name",
                 "attacking", "is_attacking", "enters_tapped")}

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish (not the clean checkpoint)")
            except Exception as e:
                notes.append(f"final post export failed: {e}")
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")

        # A1: setup
        if (pre_st is not None and kaalia_cast
                and obs["kaalia_attack_declared"]):
            ass["A1_setup_ok"] = "passed"
            notes.append(f"A1 passed: Kaalia cast and declared attacking; "
                         f"pre.json exported (P1 life pre={obs['life_pre']})")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(f"A1 failed: kaalia_cast={kaalia_cast}, "
                         f"attack_declared={obs['kaalia_attack_declared']}, "
                         f"pre_exists={pre_st is not None}")
        # A2: trigger offered
        if obs["kaalia_trigger_seen"] or obs["may_choices_seen"] > 0:
            ass["A2_trigger_offered"] = "passed"
            notes.append(f"A2 passed: Kaalia attack trigger seen on stack="
                         f"{obs['kaalia_trigger_seen']}; may-choice offers="
                         f"{obs['may_choices_seen']}")
        else:
            ass["A2_trigger_offered"] = "failed"
            notes.append("A2 failed: no Kaalia trigger on stack and no may-choice "
                         "offered")
        # A3: no stall
        if obs["stall_observed"]:
            ass["A3_no_stall"] = "failed"
            notes.append(f"A3 failed: ReplacementChoice wait with no actionable "
                         f"submission persisted >60s "
                         f"({len(obs['replacement_waits'])} waits logged)")
        elif obs["replacement_waits"]:
            ass["A3_no_stall"] = "passed"
            notes.append(f"A3 passed: {len(obs['replacement_waits'])} "
                         f"ReplacementChoice wait(s) observed, every one "
                         f"actionable, none stalled")
        else:
            ass["A3_no_stall"] = "passed"
            notes.append("A3 passed: no ReplacementChoice wait ever needed the "
                         "stall watchdog")
        # A4: angel enters tapped and attacking. Attacking membership is recorded
        # at the state level in state["combat"]["attackers"] (objects carry no
        # attacking flag); the entered angel also carries
        # entered_via_ability_source, corroborating the trigger's zone change.
        if post_st is not None:
            aoid = angel_on_bf(post_st)
            if aoid is not None:
                snap = angel_snapshot(post_st, aoid)
                wire("angel_post_snapshot", snap)
                wire("angel_object_keys", sorted(get_obj(post_st, aoid).keys()))
                attackers = (post_st.get("combat") or {}).get("attackers") or []
                angel_attacking = any(
                    a.get("object_id") == aoid
                    and (a.get("attack_target") or {}).get("data") == 1
                    for a in attackers)
                via_ability = bool(get_obj(post_st, aoid)
                                   .get("entered_via_ability_source"))
                if snap.get("tapped") and angel_attacking:
                    ass["A4_angel_enters"] = "passed"
                    notes.append(f"A4 passed: Serra Angel oid {aoid} on P0 "
                                 f"battlefield, tapped={snap.get('tapped')}, "
                                 f"listed in combat.attackers vs P1="
                                 f"{angel_attacking}, "
                                 f"entered_via_ability_source={via_ability}")
                else:
                    ass["A4_angel_enters"] = "failed"
                    notes.append(f"A4 failed: Angel on BF but tapped="
                                 f"{snap.get('tapped')} attacking(vs P1)="
                                 f"{angel_attacking} via_ability={via_ability}")
            else:
                ass["A4_angel_enters"] = "failed"
                notes.append("A4 failed: Serra Angel not on P0 battlefield in "
                             "post.json")
        else:
            ass["A4_angel_enters"] = "failed"
            notes.append("A4 failed: no post.json")
        # A5: cleanup
        if post_st is not None:
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if slen == 0 and not obs["stall_observed"]:
                ass["A5_cleanup"] = "passed"
                notes.append(f"A5 passed: post.json stack empty, game proceeding "
                             f"(turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}, wf={wf})")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"A5 failed: post stack={slen}, wf={wf}, "
                             f"stall={obs['stall_observed']}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: no post.json")
        # Verdict
        if obs["stall_observed"] or obs["entry_stall"]:
            verdict = "reproduced"
            notes.append("verdict=reproduced: the reported ReplacementChoice "
                         "stall signature" +
                         (" (stall >60s with no actionable submission)" if obs["stall_observed"] else "") +
                         ("; additionally the answered put-choice never put "
                          "the Angel onto the battlefield within 150s "
                          "(entry stall, related failure)" if obs["entry_stall"] else ""))
        elif (ass["A3_no_stall"] == "passed"
              and ass["A4_angel_enters"] == "passed"
              and ass["A5_cleanup"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: Kaalia's attack trigger "
                         "completes per Oracle (Angel enters tapped and "
                         "attacking) and the game proceeds; the reported "
                         "ReplacementChoice freeze does not occur on v0.78.0")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: setup gate never opened or the "
                         "trigger/put sequence did not complete; no trustworthy "
                         "engine behavior observed")
        run = {
            "issue": 4192,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "driver_notes": [
                "First attempt aborted: generic_decision's empty-DeclareAttackers "
                "branch ran before the custom attack block, so Kaalia never "
                "attacked; also the trigger-on-stack detector fired spuriously on "
                "the Kaalia cast (spell names Kaalia as source). Fixed: custom "
                "DeclareAttackers block moved before generic_decision; trigger "
                "detection gated on an actual attack declaration.",
                "The A4 attacking check first read non-existent object fields "
                "(attacking/is_attacking); the engine records attackers in "
                "state.combat.attackers. Fixed the evaluator; re-ran the full "
                "scenario for a consistent evidence bundle.",
            ],
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4192.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x Kaalia / 8x Serra Angel deck density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The entering card (Serra Angel) carries no replacement effects; "
                "the test targets the zero-action ReplacementChoice softlock "
                "signature during the tapped-and-attacking zone change.",
                "Not tested on the original 2026-06-23 build v0.3.0; verdict is "
                "scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x Kaalia of the Vast + 8x Serra Angel + 12x Mountain + "
                          "12x Plains + 12x Swamp (mulligan to Kaalia+Angel+3 lands; "
                          "cast Kaalia with {1}{R}{W}{B}; attack with her); "
                          "P1: 60x Forest, do-nothing",
            "contract_line": "Kaalia attacks -> trigger offers the may-choice -> yes -> "
                             "Serra Angel selected -> Angel enters tapped and attacking; "
                             "no ReplacementChoice stall; game proceeds",
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

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stall_return = False
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
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
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        if obs["stall_observed"] and not stall_return:
            stall_return = True
            say("stall signature confirmed; finishing")
            await finish()
            return
        # entry-stall watchdog: trigger answered but Angel never enters
        if ((obs["may_answered"] or obs["angel_selected"])
                and not post_exported and not obs["entry_stall"]):
            since = obs["angel_selected_at"] or obs["may_answered_at"]
            s_now = p0.latest["state"] if p0.latest else None
            if (s_now is not None and angel_on_bf(s_now) is None
                    and since is not None
                    and time.time() - since > 150):
                obs["entry_stall"] = True
                say("ENTRY STALL: put-choice answered >150s ago, Angel never entered")
                wire("entry_stall", {
                    "waiting_for": (s_now.get("waiting_for") or {}).get("type"),
                    "phase": s_now.get("phase"),
                    "stack": stack_signatures(s_now)})
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_entry_stall.json", "w") as f:
                        f.write(mid)
                except Exception as e:
                    notes.append(f"mid_entry_stall export failed: {e}")
                await finish()
                return
        # trigger-stall watchdog: Kaalia attacked but no trigger/may-choice
        # ever surfaces within 150s
        if (obs["kaalia_attack_declared"] and not obs["kaalia_trigger_seen"]
                and obs["may_choices_seen"] == 0 and not obs["entry_stall"]
                and time.time() - t_start > 300):
            pass  # folded into the global timeout; the trigger may just be
            # resolving on a later turn cycle
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} kaalia_cast={kaalia_cast} "
                f"attacked={obs['kaalia_attack_declared']} "
                f"trigger={obs['kaalia_trigger_seen']} "
                f"may={obs['may_answered']} sel={obs['angel_selected']} "
                f"entered={obs['angel_entered_at'] is not None} "
                f"repl_waits={len(obs['replacement_waits'])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
