#!/usr/bin/env python3
"""Issue #6866: Delina, Wild Mage doesn't reroll on 15-20.

Oracle (pinned v0.80.0 card-data): "Whenever Delina attacks, choose target
creature you control, then roll a d20. 1-14 | Create a tapped and attacking
token that's a copy of that creature, except it's not legendary and it has
'At end of combat, exile this token.' 15-20 | Create one of those tokens.
You may roll again."

Pinned data: the 1-14 branch is fully implemented (CopyTokenOf with
RemoveSupertype(Legendary) + granted end-of-combat exile trigger). The 15-20
branch parses as effect=Unimplemented("create") with an optional
sub_ability=Unimplemented("roll again"). The triage acceptance criteria: a
15-20 creates the token and offers an optional reroll; each further 15-20
may repeat; 1-14 ends the sequence.

The engine surfaces no die-roll result to the client (no RollResult
waiting_for; nothing in server.log), so the roll branch is identified by its
effect: 1-14 creates exactly one token; 15-20 (Unimplemented) creates none.

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  SETUP  - P0: lands, Grizzly Bears, Delina, Wild Mage on BF. P1: passive
           Forests + Llanowar Elves that chump-block Delina each turn
           (Delina 3/2 vs Elf 1/1: elf dies, Delina lives, P1 takes 0).
  ATTACK - P0 attacks with Delina alone every turn; the trigger's
           TargetSelection (2 legal targets: Bear + Delina) is answered
           with the Bear. Per attack turn record: target answered (turn),
           new P0 token oids after trigger resolution (delta 1 => 1-14,
           delta 0 => 15-20 signature), waiting_for types seen that turn.
  STOP   - once >=1 low turn (delta==1, control) and >=1 high turn
           (target answered, delta==0) are recorded; watchdog 14 attacks.

Behavioral contract:
  A1 setup_ok       pre.json: P0 PreCombatMain, Delina on BF (Bear on BF
                    preferred for the 2-target prompt path), life 20/20
  A2 trigger_fires  >=1 TargetSelection answered (target = Grizzly Bears)
  A3 low_branch     >=1 turn with exactly 1 new token: tapped, attacking,
                    is_token, copy of Bear, non-legendary, carries the
                    granted end-of-combat exile ability
  A4 high_observed  >=1 turn where the trigger fired and the initial roll
                    resolved with 0 new tokens (15-20 signature; 1-14 provably
                    creates a token per A3)
  A5 reroll_offered on a high turn the engine offered the optional
                    "you may roll again" prompt (OptionalEffectChoice)
  A6 reroll_works  accepting the reroll produced a further roll resolution
                    (a new token from a 1-14 re-roll, or a repeated prompt
                    from another 15-20)
  A7 exile_ok       on low turns the token is exiled by PostCombatMain
                    (acceptance criterion; recorded as observed)

Verdict = reproduced iff A1..A4 pass and the reroll does not actually happen
(A5 fails: never offered — the original report; or A5 passes but A6 fails:
offered yet accepting resolves nothing). A4 also documents the related gap
that the pinned 15-20 branch creates no token (Unimplemented). A7 is
informational. Blocked if no Delina attack turn completes.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6866b"
EVID_ISSUE = "6866"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
TMPD = f"/tmp/ev6866_{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(TMPD, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DELINA = "Delina, Wild Mage"
BEAR = "Grizzly Bears"
ELF = "Llanowar Elves"
MTN = "Mountain"
FOREST = "Forest"
LANDS = (MTN, FOREST)

P0_DECK = [(DELINA, 8), (BEAR, 12), (MTN, 20), (FOREST, 20)]
P1_DECK = [(FOREST, 30), (ELF, 30)]

ST = {}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
PHASES = []
CASTLOG = []
C0 = None


def reset_globals():
    global C0
    ST.clear()
    ST.update({
        "stage": "SETUP", "stop": False, "retry": False,
        "attack_turns": [], "tokens_before": {}, "deltas": {},
        "target_answers": [], "low_turn": None, "high_turn": None,
        "wf_by_turn": {}, "stack_kinds_by_turn": {},
        "exile_check": {}, "pre_exported": False,
        "reroll_prompts": [], "reroll_answers": [], "tokens_at_reroll": {},
        "mid_low_exported": False, "post_low_exported": False,
        "mid_high_exported": False, "post_high_exported": False,
        "opp_logged": False, "game_code": None,
    })
    MULLS.clear(); MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear(); WF_SEEN.clear(); SUBMITTED.clear()
    LAST_SUBMIT.clear(); LAST_SUBMIT.update({"iid": None})
    PHASES.clear(); CASTLOG.clear()
    C0 = None


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def p0_token_oids(state):
    return {str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == 0
            and o.get("is_token")}


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    turn = state.get("turn_number")
    if wf:
        ST["wf_by_turn"].setdefault(turn, [])
        if not ST["wf_by_turn"][turn] or ST["wf_by_turn"][turn][-1][0] != wf:
            ST["wf_by_turn"][turn].append(
                (wf, ((state.get("waiting_for") or {}).get("data") or {}).get("player")))
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"]})


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
                       "stage": ST["stage"]})
    # stack kinds per turn (confirm the trigger resolved)
    turn = state.get("turn_number")
    kinds = []
    for entry in state.get("stack") or []:
        k = entry.get("kind") or {}
        kinds.append(k.get("type") if isinstance(k, dict) else str(k))
    if kinds:
        ST["stack_kinds_by_turn"].setdefault(turn, set()).update(kinds)


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:260]}")
    return found


async def export_now(path, dest_dir=EVDIR):
    s = await C0.export_state()
    with open(f"{dest_dir}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = seat = None
        val = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                ref = str(d["reference"])
            if "seat" in d:
                seat = d["seat"]
            if "value" in d:
                val = d["value"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "value": val, "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": str(ch.get("text") or ch.get("label") or "")[:80]})
    return out


async def answer_delina_target(c, state, st):
    """Answer Delina's attack-trigger target prompt: target our Bear.

    On protocol 69 the prompt surfaces as waiting_for type
    'TriggerTargetSelection' (observed 2026-09-11); accept the legacy
    'TargetSelection' type as well."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        cands = candidate_info(opp, state)
        # prefer the Bear; fall back to Delina herself
        want = next((x for x in cands if x["name"] == BEAR), None)
        if not want:
            want = next((x for x in cands if x["name"] == DELINA), None)
        if not want:
            continue
        if not ST["opp_logged"]:
            ST["opp_logged"] = True
            wire("delina_target_prompt",
                 {"candidates": cands, "opportunity": opp})
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] target: unexpected prompt shape {rtype}/{spec_type}")
            wire("delina_target_unexpected_shape",
                 {"rtype": rtype, "spec_type": spec_type,
                  "candidates": cands})
            continue
        await send_interaction(c, {"interactionId": iid, "response": resp_out})
        SUBMITTED.add(iid)
        ST["target_answers"].append((state.get("turn_number"), want["name"]))
        say(f"[P0] Delina trigger targets {want['name']} "
            f"(turn {state.get('turn_number')})")
        return True
    return False


async def answer_delina_reroll(c, state, st):
    """Handle Delina's 'you may roll again' OptionalEffectChoice (15-20
    branch). Log the full opportunity once, then ACCEPT to test whether the
    reroll actually resolves. Cap answers per turn to avoid an infinite
    chain.

    Any other OptionalEffectChoice for player 0 (e.g. an unimplemented-effect
    continue prompt) is answered with the affirmative/continue choice and
    logged as such, but NOT counted as a reroll prompt for A5: the
    opportunity text is checked for a roll mention."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") != "OptionalEffectChoice":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    turn = state.get("turn_number")
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        cands = candidate_info(opp, state)
        all_text = " ".join(
            [x["text"] for x in cands] + [
                str((opp.get("data") or {}).get("prompt") or ""),
                str((opp.get("data") or {}).get("description") or ""),
                str(((wf.get("data") or {}).get("description")) or ""),
            ]).lower()
        is_reroll = "roll" in all_text
        if ("reroll", iid) not in SHAPES:
            SHAPES.add(("reroll", iid))
            wire("delina_reroll_prompt",
                 {"turn": turn, "is_reroll": is_reroll,
                  "prompt_text": all_text[:300],
                  "candidates": cands, "opportunity": opp,
                  "wf_data": wf.get("data")})
            say(f"[P0] OptionalEffectChoice (turn {turn}, "
                f"is_reroll={is_reroll}): "
                f"{[(x['choice_id'], x['value'], x['text']) for x in cands]}")
            if is_reroll:
                ST["reroll_prompts"].append((turn, iid))
                if turn not in ST["tokens_at_reroll"]:
                    ST["tokens_at_reroll"][turn] = sorted(p0_token_oids(state))
            else:
                ST.setdefault("other_may_prompts", []).append((turn, iid))
        n_answers = sum(1 for t, _, _ in ST["reroll_answers"] if t == turn)
        if n_answers >= 6:
            say(f"[P0] reroll answer cap reached on turn {turn}; not answering")
            wire("reroll_cap", {"turn": turn})
            return False
        want = next((x for x in cands
                     if str(x["value"]).lower() == "true"), None)
        if not want:
            want = next((x for x in cands
                         if any(k in x["text"].lower()
                                for k in ("yes", "roll again", "roll",
                                          "continue"))), None)
        if not want:
            say("[P0] OptionalEffectChoice: no accept choice identifiable; "
                "NOT answering")
            wire("reroll_no_accept_found", {"candidates": cands,
                                            "is_reroll": is_reroll})
            ST["stop"] = True
            ST["stage"] = "UNHANDLED_PROMPT"
            return False
        resp = (opp.get("response") or {})
        if resp.get("type") != "exactChoices":
            say(f"[P0] OptionalEffectChoice: unexpected response type "
                f"{resp.get('type')}; NOT answering")
            wire("reroll_unexpected_rtype", {"rtype": resp.get("type"),
                                            "candidates": cands})
            ST["stop"] = True
            ST["stage"] = "UNHANDLED_PROMPT"
            return False
        await send_interaction(c, {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId":
                                                         want["choice_id"]}}})
        SUBMITTED.add(iid)
        ST["reroll_answers"].append((turn, want["text"], want["value"]))
        say(f"[P0] OptionalEffectChoice ACCEPTED (turn {turn}, "
            f"is_reroll={is_reroll}, choice {want['choice_id']})")
        return True
    return False


def mulligan_choice(state, pid, name):
    lands = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
    n_lands = sum(1 for n in lands if n in LANDS)
    if n_lands >= 2 or MULLS[name] >= 3:
        return "Keep"
    MULLS[name] += 1
    return "Mulligan"


async def bottom_cards(c, pid, state, acts, protect):
    for a in acts:
        if a["type"] == "SelectCards" and \
                (state.get("waiting_for") or {}).get("type") == \
                "MulliganDecision":
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            h = hand_oids(state, pid)
            # bottom non-protected, non-land cards first
            ranked = []
            for o in h:
                nm = oname(state["objects"][o])
                if nm in protect or nm in LANDS:
                    ranked.append((1, o))
                else:
                    ranked.append((0, o))
            ranked.sort(key=lambda x: x[0])
            picks = [o for _, o in ranked[:count]]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    return False


async def discard_to_size(c, pid, state, keep_map):
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "DiscardToHandSize":
        return False
    pend = wf.get("data") or {}
    if pend.get("player") != pid:
        return False
    n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
    if n <= 0:
        return False
    h = hand_oids(state, pid)
    seen = {}
    ranked = []
    for o in h:
        nm = oname(state["objects"][o])
        seen[nm] = seen.get(nm, 0) + 1
        if seen[nm] <= keep_map.get(nm, 0):
            ranked.append((0, o))       # protected
        elif nm in LANDS:
            ranked.append((1, o))       # lands kept over extras
        else:
            ranked.append((2, o))       # extras discarded first
    ranked.sort(key=lambda x: x[0], reverse=True)
    picks = [o for _, o in ranked[:n]]
    if picks:
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{c.name} discards {len(picks)}: "
            f"{[oname(state['objects'][o]) for o in picks]}")
        return True
    return False


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def p0_tick(c, pid, state, acts, st):
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            choice = mulligan_choice(state, pid, c.name)
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    if await bottom_cards(c, pid, state, acts, {DELINA, BEAR}):
        return True
    if await discard_to_size(c, pid, state, {DELINA: 2, BEAR: 2}):
        return True
    # Delina trigger target
    if await answer_delina_target(c, state, st):
        return True
    # Delina 15-20 "you may roll again" prompt
    if await answer_delina_reroll(c, state, st):
        return True
    for a in acts:
        if a["type"] == "ChooseLegend":
            await submit_as_is(c, a)
            say("P0 ChooseLegend: submitted as-is")
            return True
    # never pass while P0 has a decision pending
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 in ("OptionalCostChoice", "TargetSelection", "TriggerTargetSelection",
               "OptionalEffectChoice", "ManaPayment", "ChooseXValue",
               "DiscardChoice", "ChooseLegend") and wplayer == pid:
        return False
    # DeclareAttackers on P0's turn: attack with Delina alone
    for a in acts:
        if a["type"] == "DeclareAttackers":
            d = copy.deepcopy(a.get("data", {}))
            delina = next((oid for oid, o in bf(state, pid)
                           if oname(o) == DELINA and not o.get("summoning_sick")
                           and not o.get("tapped")), None)
            if delina and state.get("active_player") == pid:
                d["attacks"] = [[int(delina), {"type": "Player", "data": 1}]]
                d["bands"] = []
                say(f"[P0] attacks with Delina {delina} "
                    f"(turn {state.get('turn_number')})")
            else:
                d["attacks"] = []
                d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if a["type"] == "DeclareBlockers":
            d = dict(a.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
    # main-phase development
    if is_my_main(state, pid):
        for ln in (MTN, FOREST):
            lid = find_hand(state, pid, ln)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        if not any(oname(o) == BEAR for _, o in bf(state, pid)):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                CASTLOG.append((state.get("turn_number"), BEAR))
                say("[P0] casts Bear")
                return True
        if not any(oname(o) == DELINA for _, o in bf(state, pid)):
            oid = find_hand(state, pid, DELINA)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                CASTLOG.append((state.get("turn_number"), DELINA))
                say("[P0] casts Delina")
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p1_tick(c, pid, state, acts, st):
    for a in acts:
        if a["type"] == "MulliganDecision":
            choice = mulligan_choice(state, pid, c.name)
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    if await bottom_cards(c, pid, state, acts, {ELF}):
        return True
    if await discard_to_size(c, pid, state, {ELF: 4}):
        return True
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 in ("OptionalCostChoice", "TargetSelection", "TriggerTargetSelection",
               "OptionalEffectChoice", "ManaPayment", "ChooseXValue",
               "DiscardChoice", "ChooseLegend") and wplayer == pid:
        return False
    # P1 never attacks: declare no attackers
    for a in acts:
        if a["type"] == "DeclareAttackers":
            d = copy.deepcopy(a.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
    # block every P0 attacker with a separate untapped Elf
    if wt0 == "DeclareBlockers" and wplayer == pid:
        for a in acts:
            if a["type"] != "DeclareBlockers":
                continue
            data = a.get("data", {}) or {}
            vbt = (state.get("waiting_for") or {}).get("data", {}).get(
                "valid_block_targets") or {}
            attackers = set()
            for atk_list in vbt.values():
                attackers.update(str(x) for x in atk_list)
            elves = [oid for oid, o in bf(state, pid)
                     if oname(o) == ELF and not o.get("tapped")]
            assignments = []
            used = set()
            for atk in sorted(attackers):
                for e in elves:
                    if e not in used:
                        assignments.append([int(e), int(atk)])
                        used.add(e)
                        break
            d = dict(data)
            d["assignments"] = assignments
            wire("p1_blocks", {"assignments": assignments,
                               "attackers": sorted(attackers)})
            await c.send_action({"type": "DeclareBlockers", "data": d})
            say(f"[P1] blocks: {assignments}")
            return True
    if is_my_main(state, pid):
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        n_elf = sum(1 for _, o in bf(state, pid) if oname(o) == ELF)
        if n_elf < 4:
            oid = find_hand(state, pid, ELF)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P1] casts Elf")
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    ST["game_code"] = p0.game_code
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    state_keys_logged = False
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                               (p1, p1.player_id, p1_tick)):
            if not c.latest:
                continue
            rej = drain_rejections(c)
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                LAST_SUBMIT["iid"] = None
            if rej:
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tickfn(c, pid, c.latest["state"],
                                c.latest.get("legal_actions", []), c.latest):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)
        record_phase(state)
        if not state_keys_logged:
            state_keys_logged = True
            wire("state_top_keys", {"keys": sorted(state.keys())})

        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        turn = state.get("turn_number")
        phase = state.get("phase")
        active = state.get("active_player")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            delina_bf = any(oname(o) == DELINA for _, o in bf(state, 0))
            bear_bf = any(oname(o) == BEAR for _, o in bf(state, 0))
            if delina_bf and ST.get("delina_turn") is None:
                ST["delina_turn"] = turn
                say(f"Delina on BF at turn {turn}")
            # need Delina + (ideally) Bear for the 2-target prompt path;
            # fall back to Delina-alone after 6 turns so a slow Bear draw
            # can't stall setup forever
            if is_my_main(state, 0) and delina_bf and (
                    bear_bf or (turn - (ST.get("delina_turn") or turn)) >= 6):
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["stage"] = "ATTACK"
                say(f"=== stage -> ATTACK (bear_bf={bear_bf}) ===")
        elif ST["stage"] == "ATTACK":
            # per-turn pre-attack snapshot (kept in /tmp; the interesting
            # turns are copied into the evidence dir at the end)
            if phase == "PreCombatMain" and active == 0 \
                    and turn not in ST["attack_turns"] \
                    and any(oname(o) == DELINA for _, o in bf(state, 0)):
                ST["attack_turns"].append(turn)
                await export_now(f"pre_attack_{turn}.json", dest_dir=TMPD)
                say(f"--- attack turn {turn} "
                    f"(#{len(ST['attack_turns'])}) ---")
            # token baseline at DeclareAttackers (backfill the attack-turn
            # record if PreCombatMain was missed between polls)
            if phase == "DeclareAttackers" and active == 0 \
                    and any(oname(o) == DELINA for _, o in bf(state, 0)):
                if turn not in ST["attack_turns"]:
                    ST["attack_turns"].append(turn)
                    say(f"--- attack turn {turn} "
                        f"(#{len(ST['attack_turns'])}, backfilled) ---")
                if turn not in ST["tokens_before"]:
                    ST["tokens_before"][turn] = p0_token_oids(state)
            # token delta once the trigger resolved (stack empty after the
            # target was answered), or at DeclareBlockers/CombatDamage.
            # The reroll prompt (if any) snapshots tokens BEFORE the accept,
            # so initial_delta distinguishes the first roll's branch even
            # when a later accepted reroll creates a token.
            if turn in ST["attack_turns"] and turn not in ST["deltas"] \
                    and turn in ST["tokens_before"]:
                answered = any(t == turn for t, _ in ST["target_answers"])
                stack_empty = not state.get("stack")
                if (answered and stack_empty and phase == "DeclareAttackers") \
                        or phase in ("DeclareBlockers", "CombatDamage"):
                    new = p0_token_oids(state) - ST["tokens_before"][turn]
                    ST["deltas"][turn] = sorted(new)
                    say(f"turn {turn}: token delta = {len(new)} "
                        f"(new oids {sorted(new)})")
            # branch classification per attack turn
            prompt_turns = {t for t, _ in ST["reroll_prompts"]}
            trig = "TriggeredAbility" in ST["stack_kinds_by_turn"].get(turn,
                                                                       set())
            if turn in ST["attack_turns"] and trig:
                if turn in ST["tokens_at_reroll"] \
                        and turn in ST["tokens_before"]:
                    initial = len(set(ST["tokens_at_reroll"][turn])
                                  - ST["tokens_before"][turn])
                elif turn in ST["deltas"]:
                    initial = len(ST["deltas"][turn])
                else:
                    initial = None
                if initial == 1 and ST["low_turn"] is None:
                    ST["low_turn"] = turn
                    say(f"turn {turn}: classified LOW (initial_delta=1)")
                if initial == 0 and ST["high_turn"] is None:
                    ST["high_turn"] = turn
                    say(f"turn {turn}: classified HIGH (initial_delta=0, "
                        f"prompt={'yes' if turn in prompt_turns else 'no'})")
            # exports on the first low / high turns
            if ST["low_turn"] is not None and not ST["mid_low_exported"] \
                    and turn == ST["low_turn"] \
                    and phase in ("DeclareBlockers", "CombatDamage"):
                await export_now("mid_low.json")
                ST["mid_low_exported"] = True
                say(f"=== mid_low exported (turn {turn}, phase={phase}) ===")
            if ST["low_turn"] is not None and not ST["post_low_exported"] \
                    and turn == ST["low_turn"] and phase == "PostCombatMain" \
                    and active == 0 and not state.get("stack"):
                await export_now("post_low.json")
                ST["post_low_exported"] = True
                low_oids = set(ST["deltas"].get(turn, []))
                still = low_oids & p0_token_oids(state)
                ST["exile_check"][turn] = len(still) == 0
                say(f"turn {turn}: low-turn token exiled by PostCombatMain: "
                    f"{len(still) == 0}")
            if ST["high_turn"] is not None and not ST["mid_high_exported"] \
                    and turn == ST["high_turn"] and (
                        (state.get("waiting_for") or {}).get("type")
                        == "OptionalEffectChoice"
                        or phase in ("DeclareBlockers", "CombatDamage")):
                await export_now("mid_high.json")
                ST["mid_high_exported"] = True
                say(f"=== mid_high exported (turn {turn}, "
                    f"wf={(state.get('waiting_for') or {}).get('type')}, "
                    f"phase={phase}) ===")
            if ST["high_turn"] is not None and not ST["post_high_exported"] \
                    and turn == ST["high_turn"] and phase == "PostCombatMain" \
                    and active == 0 and not state.get("stack"):
                await export_now("post_high.json")
                ST["post_high_exported"] = True
            # done once we have both branches + closing exports
            if ST["low_turn"] is not None and ST["high_turn"] is not None \
                    and ST["post_low_exported"] and ST["post_high_exported"]:
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("=== both branches observed; DONE ===")
            elif len(ST["attack_turns"]) >= 14 and not ST["stop"]:
                await export_now("mid_watchdog.json")
                obs["notes"].append(
                    f"watchdog: 14 attack turns without both branches "
                    f"(low={ST['low_turn']} high={ST['high_turn']} "
                    f"deltas={ {t: len(v) for t, v in ST['deltas'].items()} })")
                say("=== watchdog fired; stopping ===")
                ST["stage"] = "WATCHDOG"
                ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    # copy the per-turn pre-attack states for the decisive turns
    for tag, t in (("low", ST["low_turn"]), ("high", ST["high_turn"])):
        if t is not None:
            src = f"{TMPD}/pre_attack_{t}.json"
            if os.path.exists(src):
                shutil.copy(src, f"{EVDIR}/pre_{tag}.json")
                say(f"copied pre_{tag}.json (turn {t})")

    obs["notes"].append(f"attack turns: {ST['attack_turns']}")
    obs["notes"].append(f"target answers: {ST['target_answers']}")
    obs["notes"].append(
        f"token deltas: { {t: len(v) for t, v in ST['deltas'].items()} }")
    obs["notes"].append(f"low_turn={ST['low_turn']} high_turn={ST['high_turn']}")
    obs["notes"].append(f"reroll prompts: {ST['reroll_prompts']}")
    obs["notes"].append(f"reroll answers: {ST['reroll_answers']}")
    obs["notes"].append(
        f"tokens_at_reroll: { {t: len(v) for t, v in ST['tokens_at_reroll'].items()} }")
    obs["notes"].append(f"exile_check={ST['exile_check']}")
    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"P0 casts: {CASTLOG}")
    obs["notes"].append(f"stage at end: {ST['stage']}")
    serializable = {str(k): sorted(v) if isinstance(v, set) else v
                    for k, v in ST["stack_kinds_by_turn"].items()}
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"],
                   "phases": PHASES,
                   "casts": CASTLOG,
                   "attack_turns": ST["attack_turns"],
                   "target_answers": ST["target_answers"],
                   "deltas": {str(t): v
                              for t, v in ST["deltas"].items()},
                   "low_turn": ST["low_turn"],
                   "high_turn": ST["high_turn"],
                   "reroll_prompts": ST["reroll_prompts"],
                   "reroll_answers": ST["reroll_answers"],
                   "tokens_at_reroll": {str(t): v
                                        for t, v in
                                        ST["tokens_at_reroll"].items()},
                   "tokens_before": {str(t): sorted(v)
                                     for t, v in ST["tokens_before"].items()},
                   "exile_check": ST["exile_check"],
                   "wf_by_turn": {str(k): v
                                  for k, v in ST["wf_by_turn"].items()},
                   "stack_kinds_by_turn": serializable,
                   "stage": ST["stage"]}, f, indent=2)
    for n in obs["notes"]:
        say(f"NOTE: {n}")

    await p0.close()
    await p1.close()
    return obs, True


def load_env(path):
    with open(path) as f:
        return json.load(f)


def assertions_from_evidence():
    """Compute A1..A6 from the saved evidence files."""
    A = {}
    notes = []

    def env(name):
        return load_env(f"{EVDIR}/{name}")

    # A1: setup (Delina on BF; Bear preferred for the 2-target path)
    try:
        s = env("pre.json")["state"]
        bf0 = [v for v in s["objects"].values()
               if v.get("zone") == "Battlefield" and v.get("controller") == 0]
        delina_bf = any(oname(v) == DELINA for v in bf0)
        bear_bf = any(oname(v) == BEAR for v in bf0)
        ok = (s.get("phase") == "PreCombatMain"
              and s.get("active_player") == 0 and delina_bf)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(
            f"A1: phase={s.get('phase')} active={s.get('active_player')} "
            f"delina_bf={delina_bf} bear_bf={bear_bf} "
            f"life={[ (p.get('id'), p.get('life')) for p in s.get('players', [])]}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        notes.append(f"A1: not-run ({e})")

    obs = load_env(f"{EVDIR}/observations.json")
    answers = obs.get("target_answers", [])
    deltas = {int(k): v for k, v in obs.get("deltas", {}).items()}

    # A2: trigger fires
    A["A2_trigger_fires"] = "passed" if any(
        n == BEAR for _, n in answers) else "failed"
    notes.append(f"A2: target answers={answers}")

    # A3: low branch token
    low_turn = obs.get("low_turn")
    try:
        s = env("mid_low.json")["state"]
        new_oids = deltas.get(low_turn, [])
        toks = [s["objects"][o] for o in new_oids if o in s["objects"]]
        checks = []
        detail = {}
        if len(toks) == 1:
            t = toks[0]
            detail = {"name": oname(t), "tapped": t.get("tapped"),
                      "is_token": t.get("is_token"),
                      "supertypes": (t.get("card_types") or {}).get("supertypes"),
                      "zone": t.get("zone")}
            blob = json.dumps(t)
            checks = [
                oname(t) == BEAR,
                bool(t.get("tapped")),
                bool(t.get("is_token")),
                "Legendary" not in ((t.get("card_types") or {}).get("supertypes") or []),
                "exile" in blob.lower(),
            ]
        ok = len(toks) == 1 and all(checks)
        A["A3_low_branch_token"] = "passed" if ok else "failed"
        notes.append(f"A3: low_turn={low_turn} new_oids={new_oids} "
                     f"token={detail} checks={checks}")
    except Exception as e:
        A["A3_low_branch_token"] = "not-run"
        notes.append(f"A3: not-run ({e})")

    # A4: high branch observed (trigger fired, first roll resolved 15-20:
    # no token from the initial resolution)
    high_turn = obs.get("high_turn")
    trig_high = "TriggeredAbility" in (
        obs.get("stack_kinds_by_turn", {}).get(str(high_turn), []))
    A["A4_high_branch_observed"] = "passed" if high_turn is not None \
        and trig_high else "failed"
    notes.append(f"A4: high_turn={high_turn} trigger_seen={trig_high} "
                 f"tokens_at_reroll={obs.get('tokens_at_reroll', {})} "
                 f"final_deltas={ {t: len(v) for t, v in deltas.items()} }")

    # A5: reroll offered on the high turn
    prompts = obs.get("reroll_prompts", [])
    prompts_high = [p for p in prompts if p[0] == high_turn]
    A["A5_reroll_offered"] = "passed" if prompts_high else "failed"
    notes.append(f"A5: reroll prompts on high turn {high_turn}: "
                 f"{prompts_high} (all prompts: {prompts})")

    # A6: accepting the reroll actually re-rolls. Evidence of a further roll
    # resolution after the accept: a new token appears (1-14 re-roll), or a
    # further reroll prompt appears (another 15-20).
    answers_r = obs.get("reroll_answers", [])
    answers_high = [a for a in answers_r if a[0] == high_turn]
    final_delta = len(deltas.get(high_turn, [])) if high_turn else 0
    works = (final_delta > 0) or (len(prompts_high) >= 2)
    if A["A5_reroll_offered"] == "failed":
        A["A6_reroll_works"] = "not-run"
    else:
        A["A6_reroll_works"] = "passed" if works else "failed"
    notes.append(f"A6: reroll answers on high turn: {answers_high}; "
                 f"final token delta={final_delta}; "
                 f"prompts on turn={len(prompts_high)}")

    # A7: end-of-combat exile on low turns (acceptance criterion)
    try:
        exile_check = obs.get("exile_check", {})
        vals = [v for k, v in exile_check.items()]
        A["A7_exile_ok"] = "passed" if vals and all(vals) else "failed"
        notes.append(f"A7: exile_check={exile_check} (token gone by "
                     f"PostCombatMain on low turns)")
    except Exception as e:
        A["A7_exile_ok"] = "not-run"
        notes.append(f"A7: not-run ({e})")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes}, f, indent=2)
    say("assertions: " + json.dumps(A))
    for n in notes:
        say("ANOTE: " + n)
    return A, notes


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 5):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {},
                         "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            A, notes = assertions_from_evidence()
            obs["assert"] = A
            obs["notes"].extend(notes)
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs.get("assert", {}), indent=2))
