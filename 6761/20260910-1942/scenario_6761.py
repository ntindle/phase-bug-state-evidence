#!/usr/bin/env python3
"""Issue #6761: Esix, Fractal Bloom does not let you choose a creature to copy
for tokens.

Oracle (triage-verified): "The first time you would create one or more tokens
during each of your turns, you may instead choose a creature other than Esix
and create that many tokens that are copies of that creature."

Reported: no replacement choice is offered when tokens would be created.

Driver plan (protocol 69, v0.79.0, native engine, two human-client seats):
  P0: 12x Esix, Fractal Bloom + 12x Llanowar Elves + 12x Saproling Migration
      + 12x Forest + 12x Island (60)
  P1: 60x Island (draw-go)
  P0 ramps with Elves, casts Esix ({4}{G}{U} per pinned data), then casts
  Saproling Migration {1}{G} (kicker declined) during P0's main phase with
  Esix + Elves on the battlefield. The first token-creation event of P0's
  turn should offer Esix's optional replacement. The driver watches
  viewer_interaction / waiting_for for the offer from cast through resolution.
  If offered, it accepts and chooses Llanowar Elves as the copy target
  (Esix must not be a candidate), then optionally casts a second Migration
  the same turn as a control (no second offer expected).

Assertions:
  A1 setup_ok       Esix + >=1 Llanowar Elves on P0 BF at Migration cast
  A2 choice_offered Esix's optional replacement choice offered to P0 on the
                    first token creation of the turn (the reported bug is
                    that it is NOT offered)
  A3 accept_path    (if A2) choosing Llanowar Elves -> 2 Elf copies enter,
                    0 Saproling tokens, Esix excluded from candidates
  A4 second_no_offer (if A2) second Migration same turn -> no offer
  A5 cleanup        stack empty, game proceeds after the test

Verdict = reproduced iff A1 passed and A2 failed.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6761")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-1942"
EVDIR = f"{BACKFILL}/evidence/6761/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ESIX = "Esix, Fractal Bloom"
ELVES = "Llanowar Elves"
MIGRATION = "Saproling Migration"
FOREST = "Forest"
ISLAND = "Island"


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "payload": payload}, default=str) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else None


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def count_bf_name(state, pid, name):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) == name)


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        if obj_name(state, oid) == name:
            return oid
    return None


def life(state, pid):
    return state["players"][pid]["life"]


def untapped_mana(state, pid):
    """(total, green, blue) untapped mana sources for pid."""
    n = g = u = 0
    for o in bf(state, pid):
        if o.get("tapped"):
            continue
        nm = o.get("base_name") or o.get("name")
        if nm == FOREST:
            n += 1
            g += 1
        elif nm == ISLAND:
            n += 1
            u += 1
        elif nm == ELVES:
            n += 1
            g += 1
    return n, g, u


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain"))


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


def find_cast(acts, state, pid, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        if obj_name(state, d.get("object_id")) == name:
            return a
    return None


_PASSED_REV = {}


async def gated_pass_prio(c):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[c.name] = rev
            return True
    return False


def drain_inbox(ctx):
    for c, tag in ctx["clients"]:
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                rec = {"who": tag, "type": t, "at": time.time(),
                       "data": json.dumps(data, default=str)[:800]}
                ctx["rejections"].append(rec)
                wire("rejection", rec)
                say(f"[{tag}] REJECTION {t}: {rec['data'][:220]}")


def find_reference(choice):
    found = []

    def rec(node, depth=0):
        if depth > 6 or found:
            return
        if isinstance(node, dict):
            dd = node.get("data") if isinstance(node.get("data"), dict) else None
            if dd is not None and "reference" in dd:
                found.append(dd["reference"])
                return
            for v in node.values():
                rec(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                rec(v, depth + 1)

    rec(choice.get("surfaces", []))
    return found[0] if found else None


def choice_text(ch):
    bits = []
    for sf in ch.get("surfaces", []) or []:
        dd = sf.get("data", {}) or {}
        for k in ("text", "label", "value", "code"):
            v = dd.get(k)
            if v:
                bits.append(str(v))
    t = ch.get("text") or ch.get("label")
    if t:
        bits.append(str(t))
    return " | ".join(bits)


def classify_candidates(state, candidates):
    out = []
    for ch in candidates:
        ref = find_reference(ch)
        name = None
        zone = None
        if ref is not None:
            o = state["objects"].get(str(ref))
            if o:
                name = o.get("base_name") or o.get("name")
                zone = o.get("zone")
        if name is None:
            name = ch.get("label") or ch.get("name") or choice_text(ch)[:60]
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": name, "zone": zone,
                    "text": choice_text(ch)[:160]})
    return out


SUBMITTED_IIDS = set()


async def decline_kicker(c, ctx):
    """Decline Saproling Migration's kicker at the OptionalCostChoice prompt."""
    st = c.latest
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        iid = op.get("interactionId")
        if iid in SUBMITTED_IIDS:
            continue
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        chs = (resp.get("data", {}) or {}).get("choices", []) or []
        codes = {}
        for ch in chs:
            ac = [((s.get("data") or {}).get("code") or "")
                  for s in ch.get("surfaces", []) or []
                  if s.get("type") == "action"]
            vals = [((s.get("data") or {}).get("role"),
                     str((s.get("data") or {}).get("value")))
                    for s in ch.get("surfaces", []) or []
                    if s.get("type") == "value"]
            codes[ch["id"]] = {"text": choice_text(ch)[:120],
                               "action_codes": ac, "values": vals}
        if not any("decideOptionalCost" in v["action_codes"]
                   for v in codes.values()):
            continue
        wire("kicker_prompt", {"iid": iid, "choices": codes,
                              "waiting_for": st["state"].get("waiting_for")})
        pick = None
        for ch in chs:
            info = codes[ch["id"]]
            if ("accept", "false") in info["values"] or \
               ("pay", "false") in info["values"]:
                pick = ch
                break
        if pick is None:
            say("kicker prompt: no decline choice found; leaving pending")
            return False
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick["id"]}}}
        wire("kicker_decline", sub)
        await c.send_interaction(sub)
        SUBMITTED_IIDS.add(iid)
        say(f"P0 declines kicker (choice {pick['id']})")
        return True
    return False


async def scan_esix_offer(c, ctx):
    """After Migration is cast, watch for Esix's optional replacement offer.

    Handles: exactChoices accept/decline prompts, schema creature-selection
    prompts (the copy-target choice), and ReplacementChoice waits.
    Returns True if it submitted something.
    """
    st = c.latest
    if not st:
        return False
    s = st["state"]
    vi = st.get("viewer_interaction") or {}
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type") or ""
    wplayer = (wf.get("data") or {}).get("player")
    opps = vi.get("opportunities", []) or []
    if wtype == "ReplacementChoice" and wplayer == 0 and not ctx["choice_offered"]:
        ctx["choice_offered"] = True
        ctx["choice_seen_at"] = time.time()
        wire("replacement_choice_wait", {"waiting_for": wf,
                                         "n_opportunities": len(opps)})
        say("ESIX OFFER observed as waiting_for=ReplacementChoice")
    for op in opps:
        iid = op.get("interactionId")
        resp = op.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        blob = json.dumps(op).lower()
        esixy = any(k in blob for k in
                    ("esix", "fractal bloom", "instead create", "copies of",
                     "replacement"))
        if rtype == "exactChoices":
            chs = data.get("choices", []) or []
            if not chs:
                continue
            if iid in SUBMITTED_IIDS:
                continue
            is_replacement = (
                (wf.get("type") or "") == "ReplacementChoice"
                or any("choosereplacement" in choice_text(ch).lower()
                       for ch in chs))
            is_copy_target = ((wf.get("type") or "") == "CopyTargetChoice"
                              and wplayer == 0)
            if not (is_replacement or is_copy_target or esixy):
                continue  # unrelated menu (e.g. priority); ignore silently
            if is_replacement and not ctx["choice_offered"]:
                ctx["choice_offered"] = True
                ctx["choice_seen_at"] = time.time()
                say("ESIX OFFER observed (exactChoices ReplacementChoice)")
            wire("esix_offer_choices",
                 {"iid": iid,
                  "choices": [{"id": ch.get("id"),
                               "text": choice_text(ch)[:200]} for ch in chs],
                  "waiting_for": wf})
            if is_replacement:
                # Map "candidate | chooseReplacement | <i>" onto
                # waiting_for.data.candidates[i]; the apply branch is the
                # candidate whose description is not "Decline".
                import re as _re
                cands = (wf.get("data") or {}).get("candidates") or []
                idx_of = {}
                for ch in chs:
                    m = _re.search(r"chooseReplacement\s*\|\s*(\d+)",
                                   choice_text(ch))
                    if m:
                        idx_of[ch.get("id")] = int(m.group(1))
                want_decline = bool(ctx.get("migration2_cast")
                                    and ctx.get("accept_path_done"))
                pick = None
                for ch in chs:
                    i = idx_of.get(ch.get("id"))
                    if i is None or i >= len(cands):
                        continue
                    desc = (cands[i].get("description") or "").strip().lower()
                    if want_decline and desc == "decline":
                        pick = ch
                        break
                    if not want_decline and desc != "decline":
                        pick = ch
                        break
                if want_decline and pick is not None:
                    ctx["choice2_seen"] = True
                    say("SECOND Migration offer observed (control) -- "
                        "declining so the game proceeds")
                elif pick is not None and not want_decline:
                    ctx["offer_accepted"] = True
                    ctx["offer_accepted_at"] = time.time()
                    say(f"P0 ACCEPTS Esix replacement (choice {pick['id']})")
                if pick is not None:
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": pick["id"]}}}
                    wire("esix_offer_decision",
                         {"decline": want_decline, "submission": sub})
                    await c.send_interaction(sub)
                    SUBMITTED_IIDS.add(iid)
                    return True
                ctx["notes"].append(
                    f"Esix ReplacementChoice seen (iid {iid}) but no "
                    f"{'decline' if want_decline else 'apply'} candidate "
                    "identifiable; leaving pending")
                say("Esix ReplacementChoice: candidate mapping failed")
                return False
            # non-replacement exactChoices: CopyTargetChoice (the creature to
            # copy) presents as "candidate | chooseTarget".
            if (wf.get("type") or "") == "CopyTargetChoice" and wplayer == 0:
                targets = (wf.get("data") or {}).get("valid_targets") or []
                tnames = {str(t): obj_name(s, t) for t in targets}
                wire("copy_target_choice",
                     {"iid": iid, "valid_targets": tnames,
                      "choices": [{"id": ch.get("id"),
                                   "text": choice_text(ch)[:160]}
                                  for ch in chs],
                      "waiting_for": wf})
                say(f"copy-target choice: valid_targets={tnames}")
                if any(nm == ESIX for nm in tnames.values()):
                    ctx["notes"].append(
                        "Esix IS among the valid copy targets "
                        "(acceptance criterion says it must not be)")
                else:
                    ctx["notes"].append(
                        "Esix correctly excluded from valid copy targets")
                if not chs:
                    return False
                # single candidate in practice; prefer a Llanowar Elves target
                pick = chs[0]
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick["id"]}}}
                wire("copy_target_choose", sub)
                await c.send_interaction(sub)
                SUBMITTED_IIDS.add(iid)
                ctx["target_chosen"] = True
                ctx["target_chosen_at"] = time.time()
                say(f"P0 chooses copy target (choice {pick['id']}, "
                    f"targets={tnames})")
                return True
            # generic scoring fallback
            pick = None
            scored = []
            for ch in chs:
                vals = [((sf.get("data") or {}).get("role"),
                         str((sf.get("data") or {}).get("value")).lower())
                        for sf in ch.get("surfaces", []) or []
                        if sf.get("type") == "value"]
                txt = choice_text(ch).lower()
                score = 0
                if ("accept", "true") in vals or ("apply", "true") in vals or \
                   ("yes", "true") in vals:
                    score = 3
                elif any(w in txt for w in ("instead", "copy", "yes", "apply",
                                            "choose a creature")):
                    score = 2
                elif ("accept", "false") in vals or ("decline", "true") in vals \
                        or ("no", "true") in vals:
                    score = -1
                scored.append((score, ch))
            scored.sort(key=lambda x: -x[0])
            if scored and scored[0][0] > 0:
                pick = scored[0][1]
            if pick is None:
                ctx["notes"].append(
                    f"Esix offer seen (iid {iid}) but no accept choice "
                    "identifiable; leaving pending")
                say("Esix offer: could not identify accept choice")
                return False
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}
            wire("esix_offer_accept", sub)
            await c.send_interaction(sub)
            SUBMITTED_IIDS.add(iid)
            ctx["offer_accepted"] = True
            ctx["offer_accepted_at"] = time.time()
            say(f"P0 ACCEPTS Esix replacement (choice {pick['id']})")
            return True
        elif rtype == "schema":
            cands = data.get("candidates", []) or []
            if not cands:
                continue
            spec = data.get("spec", {}) or {}
            classified = classify_candidates(s, cands)
            bf_creatures = [cc for cc in classified
                            if cc["zone"] == "Battlefield"]
            if not bf_creatures:
                continue
            # A creature-selection prompt while the Esix test is live: treat
            # as the copy-target choice.
            if iid in SUBMITTED_IIDS:
                continue
            wire("copy_target_prompt",
                 {"iid": iid, "spec": spec, "candidates": classified,
                  "waiting_for": wf})
            say(f"copy-target prompt: {len(classified)} candidates")
            for cc in classified:
                say(f"  candidate {cc['choice_id']}: {cc['name']} "
                    f"(zone={cc['zone']})")
            esix_cands = [cc for cc in classified if cc["name"] == ESIX]
            if esix_cands:
                ctx["notes"].append(
                    "Esix itself IS among the copy candidates "
                    "(acceptance criterion says it must not be)")
            else:
                ctx["notes"].append(
                    "Esix correctly excluded from copy candidates")
            if not ctx["choice_offered"]:
                ctx["choice_offered"] = True
                ctx["choice_seen_at"] = time.time()
                say("ESIX OFFER observed (schema copy-target prompt)")
            if ctx.get("migration2_cast") and ctx.get("accept_path_done"):
                # control: a copy-target schema prompt here means the second
                # offer skipped the ReplacementChoice step; observe it and
                # decline via empty selection if the spec allows it.
                ctx["choice2_seen"] = True
                rtype0 = spec.get("type") if isinstance(spec, dict) else None
                spec_min = (spec.get("data", {}) or {}).get("min", 1) \
                    if isinstance(spec, dict) else 1
                say("SECOND Migration offer observed (control schema)")
                if spec_min == 0:
                    sub = {"interactionId": iid,
                           "response": {"type": rtype0 or "sequence",
                                        "data": {"choiceIds": []}}}
                    wire("control_schema_decline", sub)
                    await c.send_interaction(sub)
                    SUBMITTED_IIDS.add(iid)
                    say("declined second offer via empty selection")
                    return True
                ctx["notes"].append(
                    "second offer arrived as schema without min=0; "
                    "leaving pending")
                return False
            elves = [cc for cc in classified if cc["name"] == ELVES]
            if not elves:
                ctx["notes"].append(
                    "no Llanowar Elves candidate at copy-target prompt; "
                    "leaving pending")
                return False
            cid = elves[0]["choice_id"]
            rtype2 = spec.get("type") if isinstance(spec, dict) else None
            sub = {"interactionId": iid,
                   "response": {"type": rtype2 or "sequence",
                                "data": {"choiceIds": [cid]}}}
            wire("copy_target_submission", sub)
            await c.send_interaction(sub)
            SUBMITTED_IIDS.add(iid)
            ctx["target_chosen"] = True
            ctx["target_chosen_at"] = time.time()
            say(f"P0 chooses Llanowar Elves as the copy target ({cid})")
            return True
    return False


async def discard_if_needed(c, pid, tag, s, acts, ctx):
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type") or ""
    if "Discard" not in wtype:
        return False
    data = wf.get("data") or {}
    named = data.get("player")
    if named is not None and named != pid:
        return False
    order = {ISLAND: 0, FOREST: 1, MIGRATION: 2, ELVES: 3, ESIX: 4}
    def rank(oid):
        nm = obj_name(s, oid)
        return (order.get(nm, 5), nm or "")
    hids = sorted((str(x) for x in s["players"][pid]["hand"]), key=rank)
    n = max(0, len(hids) - 7)
    if n <= 0:
        return False
    picks = [int(x) for x in hids[:n]]
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    say(f"{tag} discards {[obj_name(s, x) for x in picks]} ({wtype})")
    return True


async def p0_tick(c, s, acts, ctx):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")
    if wtype == "OrderTriggers" and wplayer == 0:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    if await discard_if_needed(c, 0, "P0", s, acts, ctx):
        return True
    # kicker decline for Saproling Migration
    if wtype == "OptionalCostChoice" and wplayer == 0:
        if await decline_kicker(c, ctx):
            return True
    # Esix offer scanner: live from the Migration cast until resolved
    if ctx["migration1_cast"] and not ctx["esix_done"]:
        if await scan_esix_offer(c, ctx):
            return True
        # watchdog: a P0 decision pending too long
        if wplayer == 0 and wtype not in ("Priority", None):
            t0 = ctx.get("decision_pending_since")
            if t0 is None:
                ctx["decision_pending_since"] = time.time()
            elif time.time() - t0 > 180:
                ctx["notes"].append(
                    f"P0 decision '{wtype}' pending >180s with no driver "
                    "action; exporting stuck state")
                post_s = await c.export_state()
                with open(f"{EVDIR}/post_stuck.json", "w") as f:
                    f.write(post_s)
                ctx["assert"]["A2_choice_offered"] = (
                    "passed" if ctx["choice_offered"] else "failed")
                ctx["assert"]["A3_accept_path"] = "not-run"
                ctx["assert"]["A4_second_no_offer"] = "not-run"
                ctx["assert"]["A5_cleanup"] = "not-run"
                ctx["finished"] = True
                return True
        else:
            ctx["decision_pending_since"] = None
    if wtype == "DeclareAttackers" and s.get("active_player") == 0:
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await c.send_action({"type": "DeclareAttackers",
                                 "data": sub["data"]})
            return True
        return False
    # main-phase development
    if is_my_main(s, 0):
        # land drop: Forest first, else Island (retry every tick; no kept-flag)
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == FOREST:
                await c.send_action(a)
                return True
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == ISLAND:
                await c.send_action(a)
                return True
        n, g, u = untapped_mana(s, 0)
        if s.get("phase") == "PreCombatMain":
            # ramp: keep up to 2 Elves on the battlefield (only pre-test, so
            # later real casts can't pollute the token-copy assertion)
            if (not ctx["migration1_cast"]
                    and count_bf_name(s, 0, ELVES) < 2
                    and find_hand(s, 0, ELVES) is not None
                    and n >= 1 and g >= 1):
                ca = find_cast(acts, s, 0, ELVES)
                if ca:
                    await c.send_action(ca)
                    say(f"P0 casts Llanowar Elves (turn {s.get('turn_number')})")
                    return True
            # Esix
            if (find_bf(s, 0, ESIX) is None
                    and find_hand(s, 0, ESIX) is not None
                    and n >= 6 and g >= 1 and u >= 1):
                ca = find_cast(acts, s, 0, ESIX)
                if ca:
                    await c.send_action(ca)
                    ctx["esix_cast_turn"] = s.get("turn_number")
                    say(f"P0 casts Esix, Fractal Bloom (turn {s.get('turn_number')})")
                    return True
        # Saproling Migration: first cast = the test; second = control
        want_mig = False
        if (find_bf(s, 0, ESIX) is not None
                and count_bf_name(s, 0, ELVES) >= 1
                and find_hand(s, 0, MIGRATION) is not None
                and n >= 2 and g >= 1):
            if not ctx["migration1_cast"]:
                want_mig = True
                ctx["which_migration"] = 1
            elif (ctx["choice_offered"] and ctx["accept_path_done"]
                    and not ctx["migration2_cast"]
                    and s.get("turn_number") == ctx["migration1_turn"]):
                want_mig = True
                ctx["which_migration"] = 2
        if want_mig:
            ca = find_cast(acts, s, 0, MIGRATION)
            if ca:
                if ctx["which_migration"] == 1:
                    pre_s = await c.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre_s)
                    pre = json.loads(pre_s)["state"]
                    ctx["pre_state"] = pre
                    ctx["sap_before"] = count_bf_name(pre, 0, "Saproling")
                    ctx["elf_before"] = count_bf_name(pre, 0, ELVES)
                    ctx["assert"]["A1_setup_ok"] = (
                        "passed" if (find_bf(pre, 0, ESIX) is not None
                                     and count_bf_name(pre, 0, ELVES) >= 1)
                        else "failed")
                    say(f"A1 setup: Esix on BF={find_bf(pre, 0, ESIX) is not None} "
                        f"Elves={count_bf_name(pre, 0, ELVES)} "
                        f"-> {ctx['assert']['A1_setup_ok']}")
                else:
                    ctx["sap_before2"] = count_bf_name(s, 0, "Saproling")
                await c.send_action(ca)
                if ctx["which_migration"] == 1:
                    ctx["migration1_cast"] = True
                    ctx["migration1_turn"] = s.get("turn_number")
                    ctx["migration1_cast_at"] = time.time()
                    say(f"P0 casts Saproling Migration #1 (turn {s.get('turn_number')})")
                else:
                    ctx["migration2_cast"] = True
                    ctx["migration2_cast_at"] = time.time()
                    say(f"P0 casts Saproling Migration #2 control (turn {s.get('turn_number')})")
                return True
    if wtype == "Priority" and s.get("priority_player") == 0:
        return await gated_pass_prio(c)
    return False


async def p1_tick(c, s, acts, ctx):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")
    if wtype == "OrderTriggers" and wplayer == 1:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    if await discard_if_needed(c, 1, "P1", s, acts, ctx):
        return True
    if wtype == "DeclareBlockers" and s.get("active_player") == 0:
        db = find_action(acts, "DeclareBlockers")
        if db:
            sub = copy.deepcopy(db)
            sub["data"]["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            return True
        return False
    if is_my_main(s, 1):
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == ISLAND:
                await c.send_action(a)
                return True
    if wtype == "Priority" and s.get("priority_player") == 1:
        return await gated_pass_prio(c)
    return False


async def keep_mulligan(c):
    st = c.latest
    if not st:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


def new_ctx():
    return {
        "clients": [],
        "rejections": [],
        "assert": {},
        "notes": [],
        "esix_cast_turn": None,
        "migration1_cast": False,
        "migration1_turn": None,
        "migration1_cast_at": None,
        "which_migration": 0,
        "migration2_cast": False,
        "migration2_cast_at": None,
        "choice_offered": False,
        "choice_seen_at": None,
        "offer_accepted": False,
        "offer_accepted_at": None,
        "target_chosen": False,
        "target_chosen_at": None,
        "accept_path_done": False,
        "choice2_seen": False,
        "esix_done": False,
        "plain_noticed_at": None,
        "decision_pending_since": None,
        "sap_before": 0,
        "elf_before": 0,
        "sap_before2": 0,
        "pre_state": None,
        "finished": False,
    }


async def token_machine(p0, ctx):
    """Evaluate the token-creation outcome across ticks."""
    if not ctx["migration1_cast"] or ctx["esix_done"]:
        return
    st = p0.latest
    if st is None:
        return
    s = st["state"]
    sap_now = count_bf_name(s, 0, "Saproling")
    elf_now = count_bf_name(s, 0, ELVES)
    sap_delta = sap_now - ctx["sap_before"]
    elf_delta = elf_now - ctx["elf_before"]

    if ctx["choice_offered"] and not ctx["accept_path_done"]:
        # accept path: expect 2 Elf copies, 0 Saprolings
        done = elf_delta >= 2 or (
            ctx["target_chosen"]
            and time.time() - ctx["target_chosen_at"] > 45)
        if done:
            ctx["assert"]["A2_choice_offered"] = "passed"
            ok = (elf_delta >= 2 and sap_delta == 0)
            ctx["assert"]["A3_accept_path"] = "passed" if ok else "failed"
            say(f"accept path: elf_delta={elf_delta} sap_delta={sap_delta} "
                f"-> A3 {ctx['assert']['A3_accept_path']}")
            wire("accept_outcome",
                 {"elf_delta": elf_delta, "sap_delta": sap_delta,
                  "waiting_for": s.get("waiting_for"),
                  "stack": len(s.get("stack", []) or [])})
            ctx["accept_path_done"] = True
            post_s = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post_s)
            # control: second Migration may still be castable this turn
            if not (ctx["migration2_cast"]):
                say("accept path done; attempting second-Migration control")
            return

    if ctx["accept_path_done"] and ctx["migration2_cast"] \
            and ctx["assert"].get("A4_second_no_offer") is None:
        sap_delta2 = sap_now - ctx["sap_before2"]
        if ctx["choice_offered"] and ctx.get("choice2_seen"):
            ctx["assert"]["A4_second_no_offer"] = "failed"
            say("A4 FAILED: second Migration also offered the replacement")
        elif sap_delta2 >= 2:
            # second resolution produced plain tokens with no new offer
            if time.time() - ctx["migration2_cast_at"] > 20:
                ctx["assert"]["A4_second_no_offer"] = "passed"
                say("A4 passed: second Migration resolved plainly, no offer")
        if ctx["assert"].get("A4_second_no_offer") is not None:
            ctx["esix_done"] = True
            ctx["finished"] = True
            post2_s = await p0.export_state()
            with open(f"{EVDIR}/post2.json", "w") as f:
                f.write(post2_s)
            stack = s.get("stack", []) or []
            wf = s.get("waiting_for") or {}
            ctx["assert"]["A5_cleanup"] = (
                "passed" if (not stack
                             and wf.get("type") in ("Priority", None))
                else "failed")
            return
        if time.time() - ctx["migration2_cast_at"] > 240:
            ctx["notes"].append("second Migration unresolved after 240s")
            ctx["assert"]["A4_second_no_offer"] = "not-run"
            ctx["assert"]["A5_cleanup"] = "not-run"
            ctx["esix_done"] = True
            ctx["finished"] = True
            return

    if ctx["accept_path_done"] and not ctx["migration2_cast"]:
        # control not possible (turn advanced or no mana/cards); wrap up
        if (s.get("turn_number") != ctx["migration1_turn"]
                or time.time() - ctx.get("offer_accepted_at", 0) > 120):
            ctx["notes"].append(
                "second-Migration control not attempted "
                f"(turn {s.get('turn_number')} vs cast turn "
                f"{ctx['migration1_turn']})")
            ctx["assert"]["A4_second_no_offer"] = "not-run"
            stack = s.get("stack", []) or []
            wf = s.get("waiting_for") or {}
            ctx["assert"]["A5_cleanup"] = (
                "passed" if (not stack
                             and wf.get("type") in ("Priority", None))
                else "failed")
            ctx["esix_done"] = True
            ctx["finished"] = True
            return

    if not ctx["choice_offered"]:
        # plain path: the reported bug -- no offer, Saprolings enter
        if sap_delta >= 2 and ctx["plain_noticed_at"] is None:
            ctx["plain_noticed_at"] = time.time()
            say(f"plain resolution: {sap_delta} Saproling tokens entered, "
                "no Esix offer seen")
            wire("plain_resolution",
                 {"sap_delta": sap_delta, "elf_delta": elf_delta,
                  "waiting_for": s.get("waiting_for")})
        if ctx["plain_noticed_at"] is not None and \
                time.time() - ctx["plain_noticed_at"] > 20:
            ctx["assert"]["A2_choice_offered"] = "failed"
            ctx["assert"]["A3_accept_path"] = "not-run"
            ctx["assert"]["A4_second_no_offer"] = "not-run"
            post_s = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post_s)
            post = json.loads(post_s)["state"]
            stack = post.get("stack", []) or []
            wf = post.get("waiting_for") or {}
            ctx["assert"]["A5_cleanup"] = (
                "passed" if (not stack
                             and wf.get("type") in ("Priority", None))
                else "failed")
            say(f"A2 FAILED (no Esix offer); A5 {ctx['assert']['A5_cleanup']}")
            ctx["esix_done"] = True
            ctx["finished"] = True
            return
        if time.time() - ctx["migration1_cast_at"] > 600:
            ctx["notes"].append(
                "Migration #1 cast >600s ago with no token delta and no "
                "offer; marking blocked")
            ctx["assert"]["A2_choice_offered"] = "not-run"
            ctx["esix_done"] = True
            ctx["finished"] = True


async def run_game():
    ctx = new_ctx()
    _PASSED_REV.clear()
    SUBMITTED_IIDS.clear()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck((ESIX, 12), (ELVES, 12), (MIGRATION, 12),
                          (FOREST, 12), (ISLAND, 12)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    ctx["clients"] = [(p0, "P0"), (p1, "P1")]
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ctx["assert"]["A1_setup_ok"] = "not-run"
    t0 = time.time()
    last_rev = {}
    last_tick = {}
    last_status = 0.0
    try:
        while time.time() - t0 < 1500 and not ctx["finished"]:
            await asyncio.sleep(0.12)
            drain_inbox(ctx)
            for c, pid, is_p0 in ((p0, 0, True), (p1, 1, False)):
                if await keep_mulligan(c):
                    continue
                st = c.latest
                if st is None:
                    continue
                rev = st.get("state_revision", -1)
                if rev == last_rev.get(c.name) and time.time() - last_tick.get(c.name, 0) < 3:
                    continue
                s = st["state"]
                acts = st.get("legal_actions", [])
                if is_p0:
                    acted = await p0_tick(c, s, acts, ctx)
                else:
                    acted = await p1_tick(c, s, acts, ctx)
                last_rev[c.name] = st.get("state_revision", -1)
                last_tick[c.name] = time.time()
            await token_machine(p0, ctx)
            if time.time() - last_status > 60 and not ctx["finished"]:
                last_status = time.time()
                st = p0.latest
                if st:
                    s = st["state"]
                    say(f"status: turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"active={s.get('active_player')} "
                        f"waiting_for={json.dumps(s.get('waiting_for'))[:160]}")
    finally:
        await p0.close()
        await p1.close()
    for k in ("A1_setup_ok", "A2_choice_offered", "A3_accept_path",
              "A4_second_no_offer", "A5_cleanup"):
        ctx["assert"].setdefault(k, "not-run")
    return ctx


async def main():
    t0 = time.time()
    say("===== issue #6761 run =====")
    ctx = await run_game()
    dur = time.time() - t0
    ass = ctx["assert"]
    say(f"assertions: {json.dumps(ass)}")
    notes = ctx["notes"] + [
        "protocol-69 driver (v0.79.0): Esix cast via CastSpell; Saproling "
        "Migration cast with kicker declined at OptionalCostChoice "
        "(decideOptionalCost + (pay,false)); Esix offer detected via "
        "viewer_interaction exactChoices/schema prompts or "
        "waiting_for=ReplacementChoice; copy target chosen via schema "
        "{type:<spec.type>, data:{choiceIds:[...]}}.",
        "P0 deck 12x Esix, Fractal Bloom + 12x Llanowar Elves + 12x Saproling "
        "Migration + 12x Forest + 12x Island; P1 60x Island draw-go "
        "(engine accepts >4-of for custom games).",
        "Pinned card-data.json prices Esix at {4}{G}{U} (generic 4); "
        "printed cost is {3}{G}{U}. Driver pays whatever the engine asks.",
    ]
    a1 = ass.get("A1_setup_ok")
    a2 = ass.get("A2_choice_offered")
    if a1 == "passed" and a2 == "failed":
        verdict = "reproduced"
    elif a1 == "passed" and a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": 6761,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-10",
            "source": "release manifest + sha256 match of pinned verified artifacts; ServerHello mode=Full",
        },
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6761.py"),
        "decks": {
            "P0": [[ESIX, 12], [ELVES, 12], [MIGRATION, 12],
                   [FOREST, 12], [ISLAND, 12]],
            "P1": [[ISLAND, 60]],
        },
        "assertions": ass,
        "notes": notes,
        "rejections": ctx["rejections"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.79.0, not a fix claim.",
            "Discord thread (report source) is login-gated; scenario is built "
            "from the issue's triage summary and acceptance criteria.",
        ],
        "setup_line": "P0: 12x Esix, Fractal Bloom + 12x Llanowar Elves + 12x Saproling Migration + 12x Forest + 12x Island; P1: 60x Island",
        "contract_line": "First token creation of P0's turn with Esix on the battlefield offers the optional replacement: choose a creature other than Esix, create that many tokens as copies of it",
        "stats": {"states_seen": "n/a", "trigger_observations": "n/a"},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6761.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6761.py").read())
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
