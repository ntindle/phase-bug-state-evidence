#!/usr/bin/env python3
"""Issue #7179: Enrage abilities stopped resolving.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord, 2026-08-10): Cacophodon and Ripjaw Raptor fought via
Wayta, Trainer Prodigy's fight ability. Wayta's static doubles each enrage
trigger, so 2 triggers from each dino = 4 triggers on the stack. Only the
FIRST Cacophodon untap resolved; the other 3 triggers disappeared.

Oracle text (pinned card-data.json v0.84.0):
  Cacophodon ({3}{G}, 2/5): "Enrage - Whenever this creature is dealt
    damage, untap target permanent."
  Ripjaw Raptor ({2}{G}{G}, 4/5): "Enrage - Whenever this creature is dealt
    damage, draw a card."
  Wayta, Trainer Prodigy ({R}{G}{W}, 1/5, Haste):
    "{2}{G}, {T}: Target creature you control fights another target creature.
     This ability costs {2} less to activate if it targets two creatures
     you control."
    "If a creature you control being dealt damage causes a triggered ability
     of a permanent you control to trigger, that ability triggers an
     additional time."

Card-data parse state on v0.84.0 (verified 2026-09-15 before the run):
  Cacophodon trigger: DamageReceived -> SetTapState(Untap,
    target Typed[Permanent]), no Unimplemented nodes, NOT optional.
  Ripjaw Raptor trigger: DamageReceived -> Draw(1, Controller), no
    Unimplemented nodes.
  Wayta activated: Fight(subject=Creature/You, target=Another Creature),
    cost {2}{G}+Tap; the "{2} less" reduction is an
    Unimplemented(unparsed_condition) sub_ability, so the fixture pays the
    full {2}{G}+Tap.
  Wayta static: DoubleTriggers{cause: ControlledCreatureDealtDamage}.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 4x Wayta, Trainer Prodigy, 8x Cacophodon, 8x Ripjaw Raptor,
      16x Forest, 12x Mountain, 12x Plains.
  P1: 60x Forest (passive: land drops only, no attacks/blocks).

Planned line:
  P0 casts Cacophodon, Ripjaw Raptor, Wayta; keeps lands dropping.
  Once Wayta is past summoning sickness with 3+ untapped lands, P0
  activates its fight ability targeting own Cacophodon + own Ripjaw Raptor
  (answers the fight TargetSelection deliberately).
  Fight resolves: each dino is dealt damage once -> 1 enrage trigger each ->
  Wayta doubles each -> the engine offers an OrderTriggers prompt with the
  4 triggers (2 Cacophodon + 2 Ripjaw); the driver submits them in the
  offered order, placing all 4 TriggeredAbility entries on the stack.
  Driver answers the two Cacophodon untap TriggerTargetSelections with two
  deliberately chosen tapped permanents (the tapped Wayta + a tapped land
  from the activation cost); Ripjaw draws need no interaction.
  pre_fight.json is exported before activation; mid.json at peak trigger
  multiplicity; post.json when the stack empties in the same turn.

Assertions (each passed / failed / not-run):
  A1_parse          enrage triggers supported (typed Untap/Draw, no
                    Unimplemented); Wayta DoubleTriggers static present.
  A2_setup          all three creatures on P0 BF; fight activated AND
                    resolved (enrage triggers observed).
  A3_four_triggers  >=4 distinct enrage TriggeredAbility stack entries
                    observed (2 Cacophodon-sourced + 2 Ripjaw-sourced).
  A4_all_resolve    both recorded untap targets untapped in post.json AND
                    P0 hand grew by exactly 2 (two Ripjaw draws).
  A5_no_disappear   completed effects (untaps + draws) == distinct enrage
                    triggers observed.
  A6_cleanup        stack empty, waiting_for Priority, game proceeded.

Verdict rule:
  blocked        iff A2 fails (fight never activated/resolved; exact
                 trigger window unreachable).
  reproduced     iff A2 passes and A3 passes and (A4 or A5 fails): the
                 triggers reached the stack but fewer than 4 completed
                 their effects - the reported "disappeared" shape.
  not-reproduced iff A3/A4/A5/A6 all pass: all 4 triggers distinct and
                 each completed its own draw/untap.

Evidence: evidence/7179/<run-id>/pre_fight.json, mid.json, post.json,
run.json, manifest.sha256, summary.png, scenario_7179.py, wire_log.jsonl,
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
ISSUE = 7179
RUN_ID = os.environ.get("RUN_ID", "20260915-7179")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

WAYTA = "wayta, trainer prodigy"
CACO = "cacophodon"
RAPTOR = "ripjaw raptor"
FOREST = "forest"
MOUNTAIN = "mountain"
PLAINS = "plains"

P0_DECK = [(WAYTA, 4), (CACO, 8), (RAPTOR, 8),
           (FOREST, 16), (MOUNTAIN, 12), (PLAINS, 12)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "single-user",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode; data digests match "
                      "the signed manifest",
    "observed_at": "2026-09-15",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run's session under runs/20260915-7179/",
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


def ref_key(ref):
    """Normalize a candidate reference to a comparable string oid."""
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


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def tapped_perms(state, pid):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and o.get("tapped")]


def untapped_lands(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names):
            if not o.get("tapped"):
                out.append(int(oid))
    return out


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


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


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


def stack_triggers(state):
    """All TriggeredAbility stack entries with (id, source_id, source name)."""
    out = []
    for e in (state.get("stack") or []):
        kind = (e.get("kind") or {})
        if kind.get("type") == "TriggeredAbility":
            sid = e.get("source_id")
            out.append({"id": e.get("id"), "source_id": sid,
                        "source": lname(state, sid) if sid else "?"})
    return out


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",       # setup -> fight_pending -> triggers ->
                                # cleanup -> done
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "wayta_oid": None, "caco_oid": None, "raptor_oid": None,
        "wayta_cast_turn": None,
        "wayta_cast": False, "caco_cast": False, "raptor_cast": False,
        "fight_activated": False, "fight_resolved": False,
        "fight_target_stage": "none",   # none -> pending -> done
        "fight_slots_answered": [],    # (iid, slot) pairs answered
        "order_offered": None,         # trigger list from OrderTriggers
        "fight_turn": None,
        "trigger_ids_seen": [],   # distinct enrage trigger stack entry ids
        "trigger_max_simul": 0,
        "trigger_source_counts": {},  # peak breakdown
        "untap_answered": 0,
        "untap_targets": [],      # oids chosen as untap targets
        "hand_at_fight": None,
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "cleanup_from_turn": None,
        "game_code": None,
        "mid_turn": None,
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-wayta")
    p1 = PhaseClient("P1-forest")
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
                f"(turn={env['state'].get('turn_number')})")
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

    def mulligan_pending_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = (p.get("phase") or {})
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan_keep(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if any(k in tx for k in protect):
                    return 2
                if FOREST in tx or MOUNTAIN in tx or PLAINS in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            if acted(f"disc{iid}", st.get("state_revision", -1)):
                return True
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(answer_vi_choice(c, opp, pick))
            return True
        return False

    def answer_vi_choice(c, opp, choice):
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cid = choice.get("id")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cid}}}
        return sub

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def activate_ability_action(acts, state, oid):
        for a in acts:
            if a.get("type") != "ActivateAbility":
                continue
            dd = a.get("data") or {}
            if dd.get("source_id") == oid or str(a.get("_src_oid")) == str(oid):
                return a
        return None

    def candidate_oid_map(opp):
        """Map normalized ref_key -> candidate dict for an opportunity."""
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        m = {}
        for ch in chs:
            rk = ref_key(ch.get("id"))
            if rk is not None:
                m[rk] = ch
            for s in ch.get("surfaces", []) or []:
                rk2 = ref_key((s.get("data") or {}).get("reference"))
                if rk2 is not None and rk2 not in m:
                    m[rk2] = ch
        return m

    async def handle_order_triggers(c, tag, st, state):
        """Answer OrderTriggers: submit ALL trigger choice ids in the
        offered order (any permutation is legal; CR 603.3b). Records the
        offered trigger list as proof of multiplicity. After answering,
        the fight has resolved and the trigger window is open."""
        wf = wf_of(state)
        if (wf.get("type") or "") != "OrderTriggers":
            return False
        if wf_player(state) != 0:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        d = wf.get("data") or {}
        offered = [(t.get("source_name"), t.get("source_id"))
                   for t in (d.get("triggers") or [])]
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            ids = [ch.get("id") for ch in chs]
            wire("order_triggers_seen",
                 {"tag": tag, "iid": str(iid)[:16], "offered": offered,
                  "n_choices": len(ids)})
            if resp.get("type") == "schema":
                spec = data.get("spec") or {}
                stype = spec.get("type") or "sequence"
            else:
                stype = "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": ids}}}
            wire("order_triggers_answer",
                 {"tag": tag, "submission": sub})
            await c.send_interaction(sub)
            ST["answered_iids"].append(iid)
            ST["order_offered"] = offered
            ST["fight_resolved"] = True
            ST["stage"] = "triggers"
            ST["fight_turn"] = state.get("turn_number")
            say(f"[P0] OrderTriggers answered: {len(ids)} triggers "
                f"offered={offered}; trigger window OPEN")
            return True
        return False

    async def handle_fight_targets(c, tag, st, state):
        """Answer Wayta's fight TargetSelection ONE SLOT AT A TIME.
        Engine rule (CR 601.2c, confirmed in interaction.rs): each
        TargetSelection prompt carries max 1 choice; multi-choice
        sequence submissions are ignored. The prompt's
        data.selection.current_slot / current_legal_targets identify the
        slot being asked.
          slot 0 (subject: creature you control) -> own Cacophodon
          slot 1 (target: another creature)     -> own Ripjaw Raptor
        Never marks the stage done here: the stage flips to done only
        when the ActivatedAbility actually reaches the stack."""
        if ST["fight_target_stage"] != "pending":
            return False
        wf = wf_of(state)
        if (wf.get("type") or "") != "TargetSelection":
            return False
        pc = ((wf.get("data") or {}).get("pending_cast") or {})
        ab = pc.get("ability") or {}
        if ab.get("source_id") != ST["wayta_oid"]:
            return False
        if wf_player(state) != 0:
            return False
        sel = ((wf.get("data") or {}).get("selection") or {})
        slot = sel.get("current_slot")
        legal = [ref_key(t) for t in
                 (sel.get("current_legal_targets") or [])]
        want = (str(ST["caco_oid"]) if slot == 0
                else str(ST["raptor_oid"]))
        if want not in legal:
            say(f"[{tag}] fight slot {slot}: want oid {want} not in "
                f"legal {legal}; holding")
            wire("fight_slot_want_illegal",
                 {"tag": tag, "slot": slot, "want": want, "legal": legal})
            return True
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            key = (str(iid), slot)
            if key in ST["fight_slots_answered"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            wire("fight_slot_opportunity",
                 {"tag": tag, "slot": slot, "want": want, "legal": legal,
                  "iid": str(iid)[:16], "rtype": resp.get("type"),
                  "candidates": [
                      {"id": ch.get("id"),
                       "ref": ref_key([
                           (s.get("data") or {}).get("reference")
                           for s in ch.get("surfaces", []) or []]),
                       "status": (ch.get("status") or {}).get("type")}
                      for ch in (data.get("choices")
                                 or data.get("candidates") or [])]})
            cmap = candidate_oid_map(opp)
            ch = cmap.get(want)
            if ch is None:
                say(f"[{tag}] fight slot {slot}: no candidate maps to oid "
                    f"{want}; holding")
                return True
            if resp.get("type") == "schema":
                spec = data.get("spec") or {}
                stype = spec.get("type") or "sequence"
                sub = {"interactionId": iid,
                       "response": {"type": stype,
                                    "data": {"choiceIds": [ch.get("id")]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch.get("id")}}}
            wire("fight_slot_answer",
                 {"tag": tag, "slot": slot, "oid": want, "submission": sub})
            await c.send_interaction(sub)
            ST["fight_slots_answered"].append(key)
            say(f"[{tag}] fight slot {slot} answered: oid {want} "
                f"({obj_name(state, int(want))})")
            return True
        return False

    async def handle_untap_targets(c, tag, st, state):
        """Answer Cacophodon's untap target selection (TriggerTargetSelection
        or TargetSelection) with deliberately chosen tapped permanents (the
        tapped Wayta, then a tapped land). Uses the prompt's authoritative
        current_legal_targets to filter."""
        if ST["stage"] != "triggers":
            return False
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        if wtype not in ("TargetSelection", "TriggerTargetSelection"):
            return False
        # never steal the fight's own target prompts
        pc = ((wf.get("data") or {}).get("pending_cast") or {})
        if (pc.get("ability") or {}).get("source_id") == ST["wayta_oid"]:
            return False
        if wf_player(state) != 0:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        sel = ((wf.get("data") or {}).get("selection") or {})
        legal = {ref_key(t) for t in
                 (sel.get("current_legal_targets") or [])}
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            cmap = candidate_oid_map(opp)
            # prefer the tapped Wayta, then any tapped permanent not
            # already targeted; restrict to legally offered targets
            tapped = tapped_perms(state, 0)
            pref = []
            if ST["wayta_oid"] in tapped:
                pref.append(ST["wayta_oid"])
            for oid in tapped:
                if oid not in pref:
                    pref.append(oid)
            chosen = None
            for oid in pref:
                if oid in ST["untap_targets"]:
                    continue
                if str(oid) in cmap and (not legal or str(oid) in legal):
                    chosen = oid
                    break
            if chosen is None:
                say(f"[{tag}] untap target: no tapped P0 permanent among "
                    f"candidates {list(cmap.keys())[:8]}; holding")
                return True
            ch = cmap[str(chosen)]
            if rtype == "schema":
                spec = (data.get("spec") or {})
                stype = spec.get("type") or "sequence"
                sub = {"interactionId": iid,
                       "response": {"type": stype,
                                    "data": {"choiceIds": [ch.get("id")]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch.get("id")}}}
            wire("untap_target_answer",
                 {"tag": tag, "target_oid": chosen,
                  "target_name": obj_name(state, chosen),
                  "submission": sub})
            await c.send_interaction(sub)
            ST["answered_iids"].append(iid)
            ST["untap_answered"] += 1
            ST["untap_targets"].append(chosen)
            say(f"[{tag}] untap target #{ST['untap_answered']}: "
                f"{obj_name(state, chosen)} oid={chosen}")
            return True
        return False

    async def combat_empty(c, pid, tag, st, state, acts):
        rev = st.get("state_revision", -1)
        wtype = wf_of(state).get("type") or ""
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_{wtype}", rev):
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                else:
                    sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state,
                               (WAYTA, CACO, RAPTOR)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        # never pass priority while a target selection for P0 is pending
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        if wtype in ("TargetSelection", "TriggerTargetSelection") \
                and wf_player(state) == 0:
            if await handle_fight_targets(p0, "P0", st, state):
                return
            if await handle_untap_targets(p0, "P0", st, state):
                return
            return
        if wtype == "OrderTriggers" and wf_player(state) == 0:
            if await handle_order_triggers(p0, "P0", st, state):
                return
            return
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        # track creature entries
        if ST["caco_oid"] is None:
            b = bf_ids(state, 0, CACO)
            if b:
                ST["caco_oid"] = b[0]
                say(f"[P0] Cacophodon ENTERED oid={b[0]} turn={turn}")
        if ST["raptor_oid"] is None:
            b = bf_ids(state, 0, RAPTOR)
            if b:
                ST["raptor_oid"] = b[0]
                say(f"[P0] Ripjaw Raptor ENTERED oid={b[0]} turn={turn}")
        if ST["wayta_oid"] is None:
            b = bf_ids(state, 0, WAYTA)
            if b:
                ST["wayta_oid"] = b[0]
                ST["wayta_cast_turn"] = turn
                say(f"[P0] Wayta ENTERED oid={b[0]} turn={turn}")

        # track the fight ability leaving the stack = fight resolved;
        # reaching the stack at all = targets accepted -> fight stage done
        fight_on_stack = any(
            (e.get("kind") or {}).get("type") == "ActivatedAbility"
            and e.get("source_id") == ST["wayta_oid"]
            for e in (state.get("stack") or []))
        if ST["fight_activated"] \
                and ST["fight_target_stage"] == "pending" and fight_on_stack:
            ST["fight_target_stage"] = "done"
            say(f"[P0] fight ability on the stack (targets accepted) "
                f"turn={turn}")
        if ST["fight_activated"] and not ST["fight_resolved"]:
            on_stack = fight_on_stack
            trigs = [t for t in stack_triggers(state)
                     if t["source"] in (CACO, RAPTOR)]
            if trigs and not on_stack:
                ST["fight_resolved"] = True
                ST["stage"] = "triggers"
                ST["fight_turn"] = turn
                say(f"[P0] FIGHT RESOLVED turn={turn}: "
                    f"{len(trigs)} enrage triggers on stack")

        # observe enrage trigger multiplicity while in the trigger stage
        if ST["stage"] == "triggers":
            trigs = [t for t in stack_triggers(state)
                     if t["source"] in (CACO, RAPTOR)]
            for t in trigs:
                if t["id"] not in ST["trigger_ids_seen"]:
                    ST["trigger_ids_seen"].append(t["id"])
                    wire("enrage_trigger_seen",
                         {"stack_id": t["id"], "source": t["source"]})
                    say(f"[P0] enrage trigger #{len(ST['trigger_ids_seen'])}: "
                        f"{t['source']} (stack id {t['id']})")
            if len(trigs) > ST["trigger_max_simul"]:
                ST["trigger_max_simul"] = len(trigs)
                counts = {}
                for t in trigs:
                    counts[t["source"]] = counts.get(t["source"], 0) + 1
                ST["trigger_source_counts"] = counts
            # export mid at peak multiplicity (>=4) or after 4 distinct seen
            if not ST["mid_exported"] and (len(trigs) >= 4
                                          or len(ST["trigger_ids_seen"]) >= 4):
                if await export_named("mid"):
                    ST["mid_exported"] = True
                    ST["mid_turn"] = turn
            # window close: stack empty of enrage triggers after having
            # seen some, same turn
            if ST["trigger_ids_seen"] and not trigs:
                rest = [e for e in (state.get("stack") or [])]
                if not rest and not ST["post_exported"]:
                    if await export_named("post"):
                        ST["post_exported"] = True
                        ST["stage"] = "cleanup"
                        ST["cleanup_from_turn"] = turn
                        say(f"[P0] trigger window closed: "
                            f"{len(ST['trigger_ids_seen'])} distinct "
                            f"triggers seen, stack empty, turn={turn}")
                    return

        if ST["stage"] == "cleanup":
            if turn > ST.get("cleanup_from_turn", turn) \
                    and not (state.get("stack") or []):
                ST["stage"] = "done"
                return

        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # --- main-phase actions ---
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop (retry every tick; no kept flag)
            for oid in hand_ids(state, 0):
                nm = lname(state, oid)
                if nm in (FOREST, MOUNTAIN, PLAINS):
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            # cast Cacophodon
            if (not ST["caco_cast"]
                    and any(n == CACO for n in hand_lnames(state, 0))):
                ca = cast_spell_action(acts, state, 0, CACO)
                if ca and not acted("caco", rev):
                    say("[P0] casting Cacophodon")
                    await submit_as_is(p0, ca)
                    ST["caco_cast"] = True
                    return
            # cast Ripjaw Raptor
            if (not ST["raptor_cast"]
                    and any(n == RAPTOR for n in hand_lnames(state, 0))):
                ca = cast_spell_action(acts, state, 0, RAPTOR)
                if ca and not acted("raptor", rev):
                    say("[P0] casting Ripjaw Raptor")
                    await submit_as_is(p0, ca)
                    ST["raptor_cast"] = True
                    return
            # cast Wayta
            if (not ST["wayta_cast"]
                    and any(n == WAYTA for n in hand_lnames(state, 0))):
                ca = cast_spell_action(acts, state, 0, WAYTA)
                if ca and not acted("wayta", rev):
                    say("[P0] casting Wayta, Trainer Prodigy")
                    await submit_as_is(p0, ca)
                    ST["wayta_cast"] = True
                    return
            # activate the fight (Wayta past summoning sickness, 3+
            # untapped lands for the full {2}{G} cost)
            if (ST["wayta_oid"] is not None
                    and ST["caco_oid"] is not None
                    and ST["raptor_oid"] is not None
                    and not ST["fight_activated"]
                    and ST["wayta_cast_turn"] is not None
                    and turn > ST["wayta_cast_turn"]):
                ul = untapped_lands(state, 0,
                                    (FOREST, MOUNTAIN, PLAINS))
                aa = activate_ability_action(acts, state, ST["wayta_oid"])
                if aa and len(ul) >= 3 and not acted("fight", rev):
                    if not ST["pre_exported"]:
                        if await export_named("pre_fight"):
                            ST["pre_exported"] = True
                            ST["hand_at_fight"] = len(hand_ids(state, 0))
                    say("[P0] ACTIVATING Wayta fight: Caco vs Raptor")
                    await submit_as_is(p0, aa)
                    ST["fight_activated"] = True
                    ST["fight_target_stage"] = "pending"
                    ST["stage"] = "fight_pending"
                    return

        # default: pass priority (always fall through, cf. #6862)
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state, ()):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
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
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"wayta={ST['wayta_oid']} caco={ST['caco_oid']} "
                    f"raptor={ST['raptor_oid']} "
                    f"trigs_seen={len(ST['trigger_ids_seen'])} "
                    f"untap_ans={ST['untap_answered']}")
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
        for fn in ("pre_fight", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre = states.get("pre_fight")
        mid = states.get("mid")
        post = states.get("post")

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.84.0/data/card-data.json"))
            c = cd["cacophodon"]
            tr = (c.get("triggers") or [{}])[0]
            ex = tr.get("execute") or {}
            eff = ex.get("effect") or {}
            caco_ok = (tr.get("mode") == "DamageReceived"
                       and eff.get("type") == "SetTapState"
                       and (eff.get("state") or {}).get("type") == "Untap"
                       and "Unimplemented" not in json.dumps(ex))
            r = cd["ripjaw raptor"]
            rex = ((r.get("triggers") or [{}])[0].get("execute") or {})
            reff = rex.get("effect") or {}
            raptor_ok = (reff.get("type") == "Draw"
                         and "Unimplemented" not in json.dumps(rex))
            w = cd["wayta, trainer prodigy"]
            dbl = any(((s.get("mode") or {}).get("DoubleTriggers") or {})
                      .get("cause") == "ControlledCreatureDealtDamage"
                      for s in (w.get("static_abilities") or []))
            notes.append(f"A1: cacophodon_trigger_ok={caco_ok}, "
                         f"ripjaw_trigger_ok={raptor_ok}, "
                         f"wayta_double_triggers={dbl}; fight "
                         f"cost-reduction unparsed (fixture pays full "
                         f"{{2}}{{G}}+Tap)")
            ok = caco_ok and raptor_ok and dbl
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None:
            ok = (ST["wayta_oid"] is not None
                  and ST["caco_oid"] is not None
                  and ST["raptor_oid"] is not None
                  and ST["fight_activated"]
                  and len(ST["trigger_ids_seen"]) >= 2)
            notes.append(f"A2: wayta={ST['wayta_oid']} caco={ST['caco_oid']} "
                         f"raptor={ST['raptor_oid']} "
                         f"fight_activated={ST['fight_activated']} "
                         f"enrage_triggers_seen="
                         f"{len(ST['trigger_ids_seen'])}")
        else:
            ok = False
            notes.append("A2 failed: pre_fight.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: four triggers ----
        n_trig = len(ST["trigger_ids_seen"])
        ok = n_trig >= 4
        notes.append(f"A3: distinct enrage triggers seen={n_trig} "
                     f"(expect >=4); max_simultaneous="
                     f"{ST['trigger_max_simul']} "
                     f"peak_sources={ST['trigger_source_counts']}")
        ass["A3_four_triggers"] = "passed" if ok else "failed"

        # ---- A4: all resolve ----
        untaps_completed = 0
        untap_detail = []
        draws = None
        if post is not None:
            for oid in ST["untap_targets"]:
                tapped = get_obj(post, oid).get("tapped")
                untap_detail.append((oid, obj_name(post, oid), tapped))
                if tapped is False:
                    untaps_completed += 1
            if pre is not None and ST["hand_at_fight"] is not None:
                draws = len(hand_ids(post, 0)) - ST["hand_at_fight"]
            notes.append(f"A4: untap_targets={ST['untap_targets']} "
                         f"untaps_completed={untaps_completed}/2 "
                         f"(detail={untap_detail}); draws={draws} "
                         f"(expect 2)")
            ok = (untaps_completed == 2 and draws == 2)
        else:
            ok = False
            notes.append("A4 failed: post.json missing")
        ass["A4_all_resolve"] = "passed" if ok else "failed"

        # ---- A5: no disappearance ----
        if post is not None and draws is not None:
            completed = untaps_completed + draws
            ok = (completed == n_trig)
            notes.append(f"A5: completed_effects={completed} "
                         f"(untaps {untaps_completed} + draws {draws}) vs "
                         f"triggers_observed={n_trig}")
        else:
            ok = False
            notes.append("A5 failed: post.json missing or draws unmeasured")
        ass["A5_no_disappear"] = "passed" if ok else "failed"

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
            notes.append("verdict=blocked: setup (A2) failed - the fight "
                         "never activated/resolved, so the exact 4-trigger "
                         "window was unreachable")
        elif n_trig < 4 and untaps_completed + (draws or 0) == n_trig \
                and n_trig >= 2:
            verdict = "blocked"
            notes.append("verdict=blocked: Wayta's DoubleTriggers did not "
                         "produce 4 triggers (saw "
                         f"{n_trig}); the reported 4-trigger "
                         "disappearance shape is untestable without the "
                         "doubling; all observed triggers resolved")
        elif ass["A3_four_triggers"] == "passed" and (
                ass["A4_all_resolve"] == "failed"
                or ass["A5_no_disappear"] == "failed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: 4 enrage triggers reached the "
                         "stack but fewer than 4 completed their effects - "
                         "the reported 'rest just disappeared' shape")
        elif all(ass.get(k) == "passed" for k in
                 ("A3_four_triggers", "A4_all_resolve",
                  "A5_no_disappear", "A6_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: all 4 triggers distinct "
                         "and each completed its own draw/untap")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 71, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre_fight.json", "mid.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Card density is a test-harness convenience (engine accepts "
                ">4-of for custom games).",
                "Wayta's fight cost-reduction clause is Unimplemented in "
                "card data; the fixture pays the full {2}{G}+Tap cost.",
                "P1 is fully passive (land drops only) so the trigger "
                "window stays clean.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
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
        d.text((24, y), "phase-rs/phase #7179 - Enrage abilities stopped "
               "resolving", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15 - "
               "Wayta-doubled enrage triggers (Cacophodon x2 + Ripjaw x2)",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        d.text((24, y), "Report: Cacophodon + Ripjaw Raptor fought; Wayta "
               "doubled each enrage ->",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "4 triggers on the stack; only the FIRST Cacophodon "
               "untap resolved, the",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "other 3 disappeared.", fill=(200, 210, 225))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: typed Untap/Draw enrage triggers; "
                        "Wayta DoubleTriggers present",
            "A2_setup": "Wayta + Caco + Raptor on P0 BF; fight activated "
                        "and resolved",
            "A3_four_triggers": ">=4 distinct enrage triggers on the stack "
                                "(2 Caco + 2 Raptor)",
            "A4_all_resolve": "both untap targets untapped + 2 Ripjaw "
                              "draws",
            "A5_no_disappear": "completed effects == triggers observed",
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

        ds = run.get("driver_state", {}) or {}
        pre, mid, post = (states.get("pre_fight"), states.get("mid"),
                          states.get("post"))

        def life(s, pid):
            for p in (s or {}).get("players", []):
                if p.get("id") == pid:
                    return p.get("life")
            return "?"

        d.text((24, y), "Trigger multiplicity:", fill=(200, 210, 225))
        y += 24
        d.text((40, y), f"distinct enrage triggers seen: "
               f"{len(ds.get('trigger_ids_seen') or [])}; max simultaneous: "
               f"{ds.get('trigger_max_simul')}; peak sources: "
               f"{ds.get('trigger_source_counts')}", fill=(180, 195, 215))
        y += 30
        d.text((24, y), "Effect completion:", fill=(200, 210, 225))
        y += 24
        tgts = ds.get("untap_targets") or []
        if post is not None:
            det = []
            for oid in tgts:
                o = post.get("objects", {}).get(str(oid)) or {}
                det.append(f"{o.get('base_name') or o.get('name') or '?'}:"
                           f"tapped={o.get('tapped')}")
            d.text((40, y), f"untap targets: {'; '.join(det) or 'none'}",
                   fill=(180, 195, 215))
            y += 24
            h0 = ds.get("hand_at_fight")
            h1 = len([o for o in
                      (post.get("players", [{}])[0].get("hand") or [])])
            d.text((40, y), f"P0 hand at fight={h0}, post={h1} "
                   f"(draws={h1 - h0 if h0 is not None else '?'})",
                   fill=(180, 195, 215))
            y += 24
            d.text((40, y), f"life={life(post, 0)}/{life(post, 1)}; "
                   f"stack={len(post.get('stack') or [])}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "post.json MISSING", fill=(255, 90, 90))
        y += 40
        d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
               f"{ISSUE}/" + RUN_ID + "/", fill=(140, 160, 180))
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
