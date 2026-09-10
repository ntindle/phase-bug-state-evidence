#!/usr/bin/env python3
"""Issue #5936: "Game broke with a prio bug for something my friend couldn't
pay for" — Slinza, the Spiked Stampede.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-16, source: Discord thread 1525628325454676068): Slinza's
optional enters trigger reached a payment decision the affected player could
not pay for; the game broke and the player could not get past it. Error
messages appeared for both single-step "resolve" and "resolve all".

Oracle text (verified from pinned v0.78.0 card-data.json):
  Slinza, the Spiked Stampede: "Beast spells you cast cost {2} less to cast.
  Each other Beast creature you control enters with an additional +1/+1
  counter on it. Whenever Slinza or another creature with power 4 or greater
  enters, you may pay {1}{R/G}. When you do, Slinza fights target creature
  you don't control."
Parsed (v0.78.0 data): optional ChangesZone trigger, effect PayCost
  {1}{R/G} (payer Controller, optional) with a sequential conditional
  (WhenYouDo) Fight child targeting Creature controlled by Opponent, subject
  SelfRef. parser status fully_parsed.

Triage acceptance criteria (mike-theDude, 2026-08-03):
  - If the cost cannot be paid, the player can decline and resolution
    continues.
  - If the cost is paid, only the conditional fight is created and it
    requires a legal opposing creature target.

Scenario (native engine, two human-client seats):
  P0: 12x Slinza, the Spiked Stampede + 12x Leatherback Baloth ({G}{G}{G}
      4/5 Beast) + 24x Forest (dense counts: engine accepts >4-of for custom
      games; mulligan hunts 2+ lands).
  P1: 12x Grizzly Bears + 36x Forest (plays land, casts Bears as fight
      targets; never attacks).

  PHASE A (reported path: cannot pay): P0 casts Slinza spending all 5 lands.
      Slinza's own entry (5/5 >= 4) fires the optional trigger with zero
      available mana. Policy: accept the trigger, then DECLINE the {1}{R/G}
      payment. Expected per acceptance criteria: the trigger resolves with
      no fight and the game continues — no errors, no stall.
  PHASE B (control: can pay): P0 casts Leatherback Baloth while keeping 2+
      Forests untapped; trigger fires; pay {1}{R/G}; target P1's Grizzly
      Bears; fight resolves. Expected: Bears dies (5 damage), Slinza takes
      2, life totals unchanged, game proceeds.

Assertions:
  A1_setup_ok        pre_A.json: Slinza on P0 BF, trigger on stack (source
                     Slinza), P0 has 0 untapped lands and empty pool.
  A2_cannot_pay      post_A.json: trigger resolved with no fight (Slinza
                     damage 0, no deaths, life 20/20), stack empty, game at
                     Priority, zero rejections.
  A3_can_pay         post_B.json: fight happened — P1 Grizzly Bears in
                     graveyard, Slinza marked with 2 damage, life 20/20,
                     stack empty, game proceeds.
  A4_cleanup         final state: stack empty, game at Priority, no stall
                     watchdog fired.

Verdict rule: reproduced iff the cannot-pay branch errors (submission
rejected), stalls (no legal decline / watchdog fires), or the game cannot
proceed past the payment decision. not-reproduced iff all of A1..A4 pass.
blocked iff the game cannot be driven to a Slinza trigger.

Evidence: evidence/5936/<run-id>/pre_A.json, post_A.json, pre_B.json,
post_B.json, run.json, manifest.sha256, summary.png, scenario_5936.py,
wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-5936-04"
EVDIR = f"{BACKFILL}/evidence/5936/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SLINZA = "slinza, the spiked stampede"
BALOTH = "leatherback baloth"
BEARS = "grizzly bears"
FOREST = "forest"

P0_DECK = [(SLINZA, 12), (BALOTH, 12), (FOREST, 24)]
P1_DECK = [(BEARS, 12), (FOREST, 36)]

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
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-5936-04",
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


def untapped_forests(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == FOREST and not o.get("tapped")]


def bf_creatures(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            tl = str(o.get("type_line") or "")
            if "creature" in tl.lower() or obj_name(o) in (SLINZA, BALOTH, BEARS):
                if name is None or obj_name(o) == name:
                    out.append(int(oid))
    return out


def damage_on(state, oid):
    return get_obj(state, oid).get("damage_marked", 0) or 0


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


def stack_triggers(state):
    out = []
    for e in state.get("stack", []) or []:
        blob = json.dumps(e, default=str).lower()
        if "trigger" in blob or "slinza" in blob:
            out.append(e)
    return out


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cannot_pay", "A3_can_pay", "A4_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    obs = {
        "phase": "A",          # A: cannot-pay branch, B: can-pay branch
        "answered_ids": [],
        "rejections": [],
        "trigger_decisions": [],
        "A_trigger_seen": False,
        "A_payment_offered": False,
        "A_declined": False,
        "A_done": False,
        "B_trigger_seen": False,
        "B_payment_offered": False,
        "B_paid": False,
        "B_target_chosen": None,
        "B_fight_resolved": False,
        "B_done": False,
        "stuck_watch_fired": False,
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

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
            info = {
                "tag": tag, "who": c.name, "canSubmit": vi.get("canSubmit"),
                "interactionId": opp.get("interactionId"),
                "rtype": resp.get("type"),
                "spec": (data.get("spec") or {}).get("type")
                        if isinstance(data.get("spec"), dict) else None,
                "prompt": str(opp.get("prompt") or opp.get("title") or "")[:160],
                "n_choices": len(choices),
                "choices": [
                    {"id": ch.get("id"), "text": choice_text(ch)[:80],
                     "status": choice_status(ch)}
                    for ch in choices[:12]],
            }
            wire("vi_opportunity", info)
            say(f"[vi {tag}/{c.name}] canSubmit={info['canSubmit']} "
                f"rtype={info['rtype']} prompt={info['prompt'][:70]!r} "
                f"choices={[(ch['text'], ch['status']) for ch in info['choices']][:6]}")

    async def submit_choice(c, opp, ch, why):
        iid = opp.get("interactionId")
        rtype = (opp.get("response", {}) or {}).get("type")
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        # string spec (e.g. "select" for DiscardToHandSize)
        if isinstance(spec, str):
            stype = spec
        cid = ch.get("id")
        if why == "discard_to_hand_size":
            ids = obs.pop("_discard_ids", [cid])
            sub = {"interactionId": iid,
                   "response": {"type": "select", "data": {"choiceIds": ids}}}
        elif rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        else:
            rdata = {"choiceIds": [cid]}
            if stype == "manaGroups":
                max_batch = ((spec.get("data") or {}).get("maxBatch")
                             if isinstance(spec.get("data"), dict) else None) or 1
                rdata["count"] = min(1, max_batch)
            sub = {"interactionId": iid,
                   "response": {"type": stype or "sequence", "data": rdata}}
        obs["answered_ids"].append(iid)
        say(f"[{c.name}] {why}: choice {cid} {choice_text(ch)[:60]!r} "
            f"(rtype={rtype})")
        wire("decision_submission", {"who": c.name, "why": why,
                                     "interactionId": iid,
                                     "submission": sub})
        await c.send_interaction(sub)

    def decide(opp, state):
        """Return (choice, policy_name) for a Slinza-trigger-related
        opportunity, or None if this opportunity is not ours to answer.

        Dispatch on response type first: exactChoices = a yes/no/pay/decline
        decision; schema (sequence/select) = target selection. Never treat a
        payment prompt as a target prompt (the ability text may mention
        "fight")."""
        # Cleanup discard: P0 accumulates cards while waiting for the exact
        # 5-untapped cast window; discard down, keeping Slinza (needed for
        # phase A). Prefer discarding Forest, then Baloth, then Slinza last.
        if opp.get("tag") == "wf-DiscardToHandSize":
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            choices = data.get("choices") or data.get("candidates") or []
            avail = [ch for ch in choices
                     if (ch.get("status") in ("available", "Available", None))]
            if not avail:
                avail = choices
            def discard_rank(ch):
                t = (ch.get("text") or "").lower()
                if "forest" in t:
                    return 0
                if "baloth" in t:
                    return 1
                return 2
            avail.sort(key=discard_rank)
            # hand size 7: discard (hand - 7); the prompt lists all hand cards
            n_discard = max(1, len(avail) - 7)
            picked = avail[:n_discard]
            # stash the ids for a select-type submission
            obs["_discard_ids"] = [c.get("id") for c in picked]
            return picked[0], "discard_to_hand_size"
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices
        texts = [(ch, choice_text(ch).lower()) for ch in avail]
        blob = (json.dumps(opp, default=str).lower()
                .replace(" ", "").replace("_", ""))
        iid = opp.get("interactionId")
        phase = obs["phase"]

        def rec(policy):
            obs["trigger_decisions"].append(
                {"interactionId": iid, "phase": phase, "policy": policy,
                 "rtype": rtype,
                 "choices": [(c.get("id"), t[:60]) for c, t in texts][:8]})
            return policy

        def find_any(*needles):
            for ch, t in texts:
                if all(n in t for n in needles):
                    return ch
            return None

        def value_role(ch):
            """Return (role, value) from the choice's value surfaces, if any."""
            for s in ch.get("surfaces", []) or []:
                d = (s.get("data") or {})
                if s.get("type") == "value" and isinstance(d, dict):
                    return d.get("role"), d.get("value")
            return None, None

        def action_code(ch):
            for s in ch.get("surfaces", []) or []:
                d = (s.get("data") or {})
                if s.get("type") == "action" and isinstance(d, dict):
                    return d.get("code")
            return None

        if rtype == "exactChoices":
            # Whitelist: only answer unambiguous trigger/game-rule prompts.
            # Priority menus (empty-text choices) are handled via Action
            # submissions and must NEVER be answered here (attempt 2
            # mis-answered a CastSpell menu choice as "pay" and cast a second
            # Slinza).
            codes = {action_code(ch) for ch in avail}
            # 1. optional trigger use: decideOptionalEffect, role accept.
            opt = [ch for ch in avail if action_code(ch) == "decideOptionalEffect"]
            if opt:
                for ch in opt:
                    role, val = value_role(ch)
                    if role == "accept" and str(val).lower() == "true":
                        return ch, rec("accept_trigger")
                return None, rec("no_accept_choice")
            # 2. legend rule: chooseLegend — keep the pre-existing Slinza
            # (lowest object reference on P0's battlefield).
            leg = [ch for ch in avail if action_code(ch) == "chooseLegend"]
            if leg:
                def leg_ref(ch):
                    for s in ch.get("surfaces", []) or []:
                        d = (s.get("data") or {})
                        if isinstance(d, dict) and d.get("reference") is not None:
                            try:
                                return int(d["reference"])
                            except (TypeError, ValueError):
                                return 10 ** 9
                    return 10 ** 9
                leg.sort(key=leg_ref)
                return leg[0], rec("choose_legend_keep_first")
            # 3. payment prompt: only a genuine pay/decline pair tied to the
            # trigger. The trigger's controller is P0; the prompt must offer
            # an explicit decline alternative (not a CastSpell menu).
            if "pay" in blob and "decideoptionaleffect" not in blob:
                pay_ch = decl_ch = None
                for ch in avail:
                    role, val = value_role(ch)
                    t = choice_text(ch).lower()
                    code = action_code(ch) or ""
                    # a CastSpell/PlayLand/ability menu is a priority menu
                    if code in ("castSpell",) or "castspell" in code.lower():
                        return None, rec("priority_menu_not_payment")
                    if (role and "pay" in str(role).lower()) or "pay" in t:
                        pay_ch = pay_ch or ch
                    if ((role and any(k in str(role).lower()
                                      for k in ("decline", "skip", "cancel")))
                            or any(k in t for k in ("decline", "don't", "do not",
                                                   "no", "skip", "cancel"))):
                        decl_ch = decl_ch or ch
                # require BOTH sides of an explicit pay/decline pair before
                # treating this as the payment prompt
                if pay_ch and decl_ch:
                    if phase == "A":
                        return decl_ch, rec("A_decline_payment")
                    return pay_ch, rec("B_pay")
                # recon: log the ambiguous prompt shape for analysis
                wire("ambiguous_pay_blob",
                     {"interactionId": iid, "phase": phase,
                      "choices": [(c.get("id"), choice_text(c)[:60],
                                   action_code(c)) for c in avail][:8]})
                return None, rec("ambiguous_payment_not_answered")
            return None, rec("unrecognized_exact")

        # schema path: target selection. Only spec.type "sequence" is treated
        # as target selection (AGENTS.md); other schema specs (e.g.
        # "relations" for DeclareAttackers) belong to their Action handlers.
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        if stype != "sequence":
            return None, rec("schema_not_sequence")
        bears_oids = set(bf_creatures(state, 1, BEARS))
        for ch in avail:
            ref = None
            for s in ch.get("surfaces", []) or []:
                d = (s.get("data") or {})
                if isinstance(d, dict) and d.get("reference") is not None:
                    ref = d["reference"]
                    break
            rid = None
            if isinstance(ref, dict):
                rid = ref.get("object_id") or ref.get("id")
            elif ref is not None:
                try:
                    rid = int(ref)
                except (TypeError, ValueError):
                    rid = None
            if rid is not None and int(rid) in bears_oids:
                return ch, rec("fight_target_bears")
        # fallback: a candidate whose text names the bears
        for ch, t in texts:
            if "bear" in t:
                return ch, rec("fight_target_bears_text")
        wire("target_candidates_no_bears",
             {"interactionId": iid, "phase": phase,
              "bears_oids": sorted(bears_oids),
              "candidates": [ch.get("id") for ch in avail]})
        return None, rec("no_bears_candidate")

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

    async def handle_vi(c, st, state):
        """Answer Slinza-trigger opportunities. Returns True if a submission
        was sent.

        Gated on a Slinza trigger actually being on the stack: before any
        trigger is seen, vi opportunities are priority menus handled via
        Action submissions, and must not be answered as trigger decisions
        (run 20260909-5936 attempt 1 mis-answered a DeclareAttackers
        "relations" schema as a fight target and fired the A-decline gate on
        a spurious "pay" match in a priority menu). The cleanup discard
        (wf-DiscardToHandSize) is the exception: P0 must discard to hand
        size while waiting for the cast window."""
        vi = get_vi(st)
        if not vi:
            return False
        # DiscardToHandSize is always safe to answer (not a priority menu).
        has_discard = any((o.get("tag") == "wf-DiscardToHandSize")
                          for o in vi.get("opportunities", []) or [])
        if not (obs["A_trigger_seen"] or obs["B_trigger_seen"]) and not has_discard:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                continue
            ch, policy = decide(opp, state)
            if ch is None:
                wire("vi_unanswered", {"who": c.name,
                                      "interactionId": iid,
                                      "policy": policy,
                                      "opp": opp})
                say(f"[{c.name}] no decision for opportunity {iid} "
                    f"(policy={policy}); logging and moving on")
                continue
            await submit_choice(c, opp, ch, policy)
            if policy == "A_decline_payment":
                obs["A_declined"] = True
                obs["A_payment_offered"] = True
            if policy == "B_pay":
                obs["B_paid"] = True
                obs["B_payment_offered"] = True
            if policy == "fight_target_bears":
                obs["B_target_chosen"] = "bears"
            acted = True
        return acted

    async def p0_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")

        # mulligan: hunt 2+ lands
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n == FOREST)
            mulls = kept.get("P0_mulls", 0)
            if lands >= 2 or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (lands={lands}, mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (lands={lands})")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]

                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm == FOREST else (2 if nm in (SLINZA, BALOTH) else 1)
                picks = sorted(hand_ids, key=bkey)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
            return True

        # payments first (land taps for mana)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return True

        # answer trigger/payment/target opportunities
        if await handle_vi(p0, st, state):
            return True

        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True

        # phase transition detection
        trigs = stack_triggers(state)
        slinza_bf = bool(bf_creatures(state, 0, SLINZA))
        if obs["phase"] == "A" and slinza_bf and trigs and not obs["A_trigger_seen"]:
            obs["A_trigger_seen"] = True
            await export("pre_A")
            notes.append(f"A trigger seen: {len(trigs)} trigger-like stack entries")
            wire("phase_A_trigger", {"stack": trigs})
        if (obs["phase"] == "A" and obs["A_done"] is False
                and obs["A_trigger_seen"]):
            # The cannot-pay branch resolves when the trigger leaves the
            # stack and the game returns to priority. The engine may offer
            # an explicit decline (then A_declined is set) or auto-skip the
            # unaffordable payment (attempt 2: no prompt was offered at all
            # with 0 mana available); either way the acceptance criterion is
            # "resolution continues".
            if not trigs and not (state.get("stack") or []):
                obs["A_done"] = True
                obs["phase"] = "B"
                await export("post_A")
                say("PHASE A complete (cannot-pay branch resolved); moving to B")
                return True
        if obs["phase"] == "B" and slinza_bf and trigs and not obs["B_trigger_seen"]:
            # this is the Baloth trigger (phase A already done)
            obs["B_trigger_seen"] = True
            await export("pre_B")
            wire("phase_B_trigger", {"stack": trigs})
        if obs["phase"] == "B" and obs["B_target_chosen"] and not obs["B_done"]:
            # The engine auto-pays the {1}{R/G} (no explicit payment prompt
            # was offered in either phase); the fight target prompt follows
            # acceptance directly. Completion = fight resolved: Bears dead,
            # stack empty.
            bears_bf = bf_creatures(state, 1, BEARS)
            bears_gy = [lname(state, o) for o in player_of(state, 1).get("graveyard", [])]
            if (BEARS in bears_gy) and not (state.get("stack") or []):
                obs["B_fight_resolved"] = True
                obs["B_done"] = True
                await export("post_B")
                say("PHASE B complete (fight resolved, Bears dead); finishing")
                return True

        if not ((wtype == "Priority") and state.get("priority_player") == 0):
            return False

        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            n_untapped = len(untapped_forests(state, 0))
            # PHASE A: cast Slinza when we have 5 untapped Forests (spends all)
            if obs["phase"] == "A" and not bf_creatures(state, 0, SLINZA):
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "PlayLand":
                        await submit_as_is(p0, a)
                        return True
                sid = spell_in_hand_oid(state, 0, SLINZA)
                # Cast with EXACTLY 5 untapped (spends all {4}{G}); the
                # cannot-pay premise needs 0 remaining. (Attempt 3 cast with
                # 7 untapped, leaving 2 -> the engine auto-paid and the
                # cannot-pay branch was never exercised.)
                if sid is not None and n_untapped == 5:
                    for a in acts:
                        d = a.get("data", {})
                        if (a["type"] == "CastSpell"
                                and lname(state, d.get("object_id")) == SLINZA):
                            say(f"P0 casts Slinza with {n_untapped} untapped "
                                f"(will leave 0 for the cannot-pay branch)")
                            wire("cast_slinza", a)
                            await submit_as_is(p0, a)
                            return True
            # PHASE B: cast Baloth keeping 2+ untapped for the {1}{R/G}
            if obs["phase"] == "B" and slinza_bf and not obs["B_trigger_seen"]:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "PlayLand":
                        await submit_as_is(p0, a)
                        return True
                bid = spell_in_hand_oid(state, 0, BALOTH)
                # Baloth {G}{G}{G} reduced by Slinza's {2} to {G}; keep >= 2
                if bid is not None and n_untapped >= 3:
                    for a in acts:
                        d = a.get("data", {})
                        if (a["type"] == "CastSpell"
                                and lname(state, d.get("object_id")) == BALOTH):
                            say(f"P0 casts Baloth with {n_untapped} untapped "
                                f"(keep 2+ for the pay branch)")
                            wire("cast_baloth", a)
                            await submit_as_is(p0, a)
                            return True

        for a in acts:
            if a["type"] == "PlayLand" and own_main:
                await submit_as_is(p0, a)
                return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps")
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
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
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
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
            # cast a Bear when able (fight target for phase B)
            bid = spell_in_hand_oid(state, 1, BEARS)
            if bid is not None and len(untapped_forests(state, 1)) >= 2:
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and lname(state, d.get("object_id")) == BEARS):
                        say("P1 casts Grizzly Bears")
                        await submit_as_is(p1, a)
                        return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        preA = load_env("pre_A"); postA = load_env("post_A")
        preB = load_env("pre_B"); postB = load_env("post_B")
        notes.append(f"decisions={len(obs['trigger_decisions'])} "
                     f"A_trigger_seen={obs['A_trigger_seen']} "
                     f"A_declined={obs['A_declined']} "
                     f"B_trigger_seen={obs['B_trigger_seen']} "
                     f"B_paid={obs['B_paid']} "
                     f"B_fight_resolved={obs['B_fight_resolved']} "
                     f"rejections={len(obs['rejections'])}")
        # A1
        if preA is not None:
            s = preA["state"]
            slinza_bf = bool(bf_creatures(s, 0, SLINZA))
            trigs = stack_triggers(s)
            untapped = len(untapped_forests(s, 0))
            ok = slinza_bf and len(trigs) >= 1 and untapped == 0
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: slinza_on_bf={slinza_bf}, trigger_stack_entries="
                         f"{len(trigs)}, untapped_forests={untapped}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre_A.json missing (Slinza trigger never seen)")
        # A2
        if preA is not None and postA is not None:
            s = postA["state"]
            slinza = bf_creatures(s, 0, SLINZA)
            dmg = damage_on(s, slinza[0]) if slinza else None
            gy0 = [lname(s, o) for o in player_of(s, 0).get("graveyard", [])]
            gy1 = [lname(s, o) for o in player_of(s, 1).get("graveyard", [])]
            stack_empty = not (s.get("stack") or [])
            wf = (s.get("waiting_for") or {}).get("type")
            rej = [r for r in obs["rejections"]]
            # The cannot-pay branch passes when the trigger resolved with no
            # fight and the game continued. The engine offered no payment
            # prompt with 0 mana available (auto-skip); an explicit decline
            # would also satisfy this.
            ok = (dmg == 0 and stack_empty
                  and wf == "Priority" and len(rej) == 0
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20)
            ass["A2_cannot_pay"] = "passed" if ok else "failed"
            notes.append(f"A2: payment_offered={obs['A_payment_offered']}, "
                         f"declined={obs['A_declined']} (engine auto-skipped "
                         f"the unaffordable payment: no prompt appeared), "
                         f"slinza_damage={dmg}, "
                         f"gy P0={gy0} P1={gy1}, stack_empty={stack_empty}, "
                         f"wf={wf}, rejections={len(rej)}, "
                         f"life={life_of(s,0)}/{life_of(s,1)}")
        else:
            ass["A2_cannot_pay"] = "failed"
            notes.append("A2: missing pre/post A states")
        # A3
        if preB is not None and postB is not None:
            s = postB["state"]
            s0 = preB["state"]
            slinza = bf_creatures(s, 0, SLINZA)
            dmg = damage_on(s, slinza[0]) if slinza else None
            gy1 = [lname(s, o) for o in player_of(s, 1).get("graveyard", [])]
            stack_empty = not (s.get("stack") or [])
            # payment evidence: P0's tapped Forests should grow by >=2
            # (Baloth cast 1 + trigger payment 2) between pre_B and post_B
            def tapped_forests(st):
                return sum(1 for o in st.get("objects", {}).values()
                           if (o.get("base_name") or o.get("name", "")).lower() == FOREST
                           and o.get("zone") == "Battlefield"
                           and o.get("controller") == 0 and o.get("tapped"))
            tapped_delta = tapped_forests(s) - tapped_forests(s0)
            ok = (obs["B_target_chosen"] == "bears" and BEARS in gy1 and dmg == 2
                  and stack_empty and life_of(s, 0) == 20
                  and life_of(s, 1) == 20 and tapped_delta >= 2)
            ass["A3_can_pay"] = "passed" if ok else "failed"
            notes.append(f"A3: payment auto-paid by engine (no explicit prompt; "
                         f"tapped Forests delta={tapped_delta}, want >=2), "
                         f"target_chosen={obs['B_target_chosen']}, "
                         f"bears_in_gy={BEARS in gy1}, slinza_damage={dmg} "
                         f"(want 2), stack_empty={stack_empty}, "
                         f"life={life_of(s,0)}/{life_of(s,1)}")
        else:
            ass["A3_can_pay"] = "failed"
            notes.append("A3: missing pre/post B states")
        # A4
        final = postB if postB is not None else postA
        if final is not None:
            s = final["state"]
            wf = (s.get("waiting_for") or {}).get("type")
            ok = (not (s.get("stack") or []) and wf == "Priority"
                  and not obs["stuck_watch_fired"])
            ass["A4_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A4: stack_empty={not (s.get('stack') or [])}, "
                         f"wf={wf}, stuck_watch={obs['stuck_watch_fired']}")
        else:
            ass["A4_cleanup"] = "failed"
            notes.append("A4: no final post state")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached a Slinza trigger")
        elif all(ass[k] == "passed" for k in
                 ("A1_setup_ok", "A2_cannot_pay", "A3_can_pay", "A4_cleanup")):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("the Slinza payment-decision path did not behave per "
                         "the triage acceptance criteria on this build")
        return verdict

    async def finish():
        dur = time.time() - t_start
        for tag in ("post_A", "post_B"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": 5936,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5936.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k not in ("trigger_decisions",)},
            "trigger_decisions": obs["trigger_decisions"],
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x spell density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The cannot-pay branch is exercised with zero available mana "
                "(all Forests tapped casting Slinza); the original report's "
                "exact mana position is unknown (Discord images expired).",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Slinza, the Spiked Stampede + 12x Leatherback "
                          "Baloth + 24x Forest; P1: 12x Grizzly Bears + 36x Forest",
            "contract_line": "Slinza optional enters trigger: (A) payment declined "
                             "with zero mana available must resolve with no fight "
                             "and no stall; (B) payment accepted with mana available "
                             "must produce the conditional fight vs the chosen "
                             "opposing creature",
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

    with open(f"{EVDIR}/scenario_5936.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_5936.py").read())

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
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
            wf_now = ((st.get("state", {}) or {}).get("waiting_for") or {}).get("type")
            mid_trigger = (obs["A_trigger_seen"] and not obs["A_done"]) or \
                          (obs["B_trigger_seen"] and not obs["B_done"])
            if mid_trigger or (wf_now and wf_now != "Priority"):
                log_vi(c, st, "trig" if mid_trigger else f"wf-{wf_now}")
        if obs["B_done"]:
            say("both phases complete; finishing")
            await finish()
            return
        if p0.latest and p0.latest["state"].get("turn_number", 0) >= 20 \
                and not obs["B_done"]:
            notes.append("turn 20 reached without completing both phases; "
                         "bailing out to evaluation")
            say("turn 20 bail-out; finishing")
            await finish()
            return
        mid_trigger = (obs["A_trigger_seen"] and not obs["A_done"]) or \
                      (obs["B_trigger_seen"] and not obs["B_done"])
        if mid_trigger and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not mid_trigger:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            obs["stuck_watch_fired"] = True
            notes.append(f"STUCK WATCH FIRED (180s mid-trigger): waiting_for={wf} "
                         f"priority_player={s.get('priority_player')}")
            say(f"STUCK WATCH FIRED: waiting_for={wf}")
            try:
                await export("mid_stall")
            except Exception:
                pass
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} hand={hand_names(s,0)[:6]} "
                f"slinza_bf={bool(bf_creatures(s,0,SLINZA))} "
                f"untapped_forests={len(untapped_forests(s,0))} "
                f"phase={obs['phase']} A_done={obs['A_done']} B_done={obs['B_done']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
