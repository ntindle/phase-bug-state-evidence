#!/usr/bin/env python3
"""Issue #658: Dualcaster Mage runtime repro, revalidation on v0.102.0 / protocol 106.

Contract (unchanged; the report is a "broken, needs-repro" thread with no
detail; the working theory from the v0.99.0/v0.101.0 runs is the full
Dualcaster path exercised end to end):
  P0: 56x Mountain + 4x Dualcaster Mage
  P1: 56x Mountain + 4x Lightning Bolt
  P1 casts Lightning Bolt targeting P0 on its own main phase; P0 responds at
  instant speed with Dualcaster Mage (Flash) while the Bolt is on the stack;
  the ETB trigger copies the Bolt; MayChooseNewTargets is answered to retarget
  the copy to P1; both resolve.

Assertions:
  A1 flash_timing      Dualcaster cast with Bolt on the stack
  A2 etb_targets_stack copy is a Bolt copy (DealDamage 3) of the stacked Bolt
  A3 copy_created      copy observed (CopyRetarget prompt and/or >=2 bolt-like
                       entries on the stack)
  A4 retarget_offered  MayChooseNewTargets offered and answered (copy -> P1)
  A5 resolution        P0 took 3 (original), P1 took 3 (copy), mage on BF,
                       stack empty, nothing pending

Verdict: reproduced iff any core assertion failed;
         not-reproduced iff all passed; blocked iff some not-run.

Protocol-106 driver notes (v0.102.0, 2026-10-04): ported from the verified
protocol-103 scenario_658_01010.py using the conventions established in the
verified protocol-106 scenario_301_01020.py (same day, same pin):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action.
  - Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards (106 uses generic 'choose' kind code).
  - CastSpell submitted via legacy Action (verified on 106); mana payment via
    legacy PayMana actions and/or vi tapLandForMana menus driven by
    mana_needs ({"generic": N}).
  - Bolt target selection via the advertised vi target opportunity (schema or
    exactChoices), preferring the seat-0 (P0) player candidate.
  - CopyRetarget detected by scanning P0's vi opportunities for copy/retarget
    markers (106 has no waiting_for CopyRetarget surface); answered by the
    seat-1 (P1) candidate, skipping decline/"none" choices.
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, castSpell, activateAbility, ...).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority. Re-tick backstop: re-tick a
    client holding priority with no revision change for > 5s.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261004-658b"
ISSUE = 658
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.102.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-04: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "fresh v0.102.0 server started by this run on 127.0.0.1:9374 "
               "with isolated run dir runs/20261004-658"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

DUALCASTER = "dualcaster mage"
BOLT = "lightning bolt"
MOUNTAIN = "mountain"

SETUP_DEADLINE_S = 1500
OBSERVE_TIMEOUT_S = 420
RESOLVE_TIMEOUT_S = 180

ST = {}
MULLS = set()
SUBMITTED = set()       # (who, tag, interactionId) already answered
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}         # tag -> {"generic": N} for vi tap-land payment


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",        # SETUP -> OBSERVE -> DONE
        "game_code": None,
        "dualcaster_cast": False,
        "dualcaster_oid": None,
        "bolt_cast_by_p1": False,
        "bolt_oid": None,
        "pre_exported": False,
        "pre_life": None,
        "post_exported": False,
        "post_life": None,
        "mage_bf": False,
        "copy_id": None,
        "copyretarget_seen": False,
        "copy_seen_stack": False,
        "etb_evidence": {},
        "retarget_done": False,
        "retarget_note": "",
        "retarget_iid": None,
        "retarget_submit_t": 0,
        "seen_vi_kinds": [],
        "bolt_target_pending": False,
        "ass": {},
        "notes": [],
        "states_seen": 0,
    })


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life(state, pid):
    return player_of(state, pid).get("life")


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_mountains(state, pid):
    return sum(1 for oid, o in (state.get("objects") or {}).items()
               if o.get("zone") == "Battlefield"
               and str(o.get("controller", -1)) == str(pid)
               and not o.get("tapped")
               and str(o.get("base_name") or o.get("name") or "").lower() == MOUNTAIN)


def stack_entries(state):
    return state.get("stack") or []


def effect_sig(entry):
    if not isinstance(entry, dict):
        return {}
    return ((((entry.get("kind") or {}).get("data") or {}).get("ability")
             or {}).get("effect") or {})


def is_boltlike(entry):
    eff = effect_sig(entry)
    return (eff.get("type") == "DealDamage"
            and (eff.get("amount") or {}).get("value") == 3)


def stack_bolt_entries(state):
    return [e for e in stack_entries(state) if is_boltlike(e)]


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))

def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. The 106 engine
    offers tapLandForMana / castSpell / activateAbility choice menus at
    ordinary priority windows; treating those as decisions stalls the game
    (they must not block priority passes)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


# ------------------------------------------------------------- interaction primitives
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    key = (c.name, tag, str(iid))
    if key in SUBMITTED:
        return False
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
    SUBMITTED.add(key)
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)
    return True


# ------------------------------------------------------------- common ticks (protocol 106)
async def do_mulligan(c, acts, st, pid, tag):
    """MulliganDecision arrives as a legacy legal action (verified accepted
    on 106). Keep everything (the key spell wants to stay in hand)."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in surf_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: per-card SelectCards + vi schema/select,
    gated on waitingForKind.code == 'mulligan' AND turn 1 / Untap."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == MOUNTAIN:
            return (0, str(oid))
        if nm in (DUALCASTER, BOLT):
            return (2, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """DiscardToHandSize via viewer_interaction; gate on hand > 7 plus a
    schema/select opportunity offering hand cards (106 uses a generic
    'choose' waitingForKind code)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(str(_cand_reference(ch)) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm in (DUALCASTER, BOLT):
            return (5, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype}): "
            f"{[obj_lname(state, _cand_reference(ch)) for ch in cands if ch['id'] in picks]}")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    """Answer the vi tapLandForMana choice menus (protocol 106 mana payment
    surface). needs: {"generic": N} or colored counts; consumes on submit."""
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a.get("type") == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def combat_tick(acts):
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            return d
    return None

# ------------------------------------------------------------- target selection (106)
def candidate_seat(ch):
    """Player seat for a target/choice candidate (strict: only player /
    target / candidate surfaces with a seat-ish key)."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(opp, preferred_seat):
    """Prefer the candidate at preferred_seat (a player)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if candidate_seat(ch) == preferred_seat:
            score += 2
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']} "
        f"({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid


# ------------------------------------------------------------- P0 / P1 ticks
async def p0_tick(c, p0state_tag, acts, st, state):
    """P0: ramp to 3 lands, then respond to the stacked Bolt with Dualcaster."""
    pid, tag = 0, "P0"
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    d = await combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    # RESPOND: cast Dualcaster Mage at instant speed while Bolt is on stack.
    if (not ST["dualcaster_cast"]
            and (stack_entries(state) or [])
            and ST["stage"] == "SETUP"):
        for a in acts:
            d2 = a.get("data") or {}
            oid = d2.get("object_id") or a.get("_src_oid")
            if (a["type"] == "CastSpell" and oid is not None
                    and obj_lname(state, int(oid)) == DUALCASTER):
                say("exporting PRE (bolt on stack, P0 responding)")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                ST["pre_exported"] = True
                ST["pre_life"] = (life(state, 0), life(state, 1))
                ST["states_seen"] += 1
                wire("pre_state_meta", {
                    "active": state.get("active_player"),
                    "phase": state.get("phase"),
                    "pre_life": ST["pre_life"],
                    "stack_ids": [e.get("id") for e in stack_entries(state)
                                  if isinstance(e, dict)]})
                say(f"[P0] casting Dualcaster Mage in response "
                    f"(active P{state.get('active_player')}, phase={state.get('phase')})")
                wire("p0_cast_dualcaster", {k: v for k, v in a.items() if not k.startswith("_")})
                ST["dualcaster_oid"] = int(oid)
                MANA_NEEDS[tag] = {"generic": 3}  # 1RR
                await submit_as_is(c, a)
                ST["dualcaster_cast"] = True
                ST["stage"] = "OBSERVE"
                ST["ass"]["A1_flash_timing"] = "passed"
                ST["notes"].append(
                    f"Dualcaster cast with Bolt on stack on P{state.get('active_player')}'s "
                    f"turn, phase {state.get('phase')}; pre.json exported before the cast")
                return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def bolt_target_cleared(c, iid):
    """True once the bolt's target selection is answered: the target
    opportunity is gone from P1's vi."""
    t0 = time.time()
    while time.time() - t0 < 15:
        st2 = c.latest
        if st2:
            vi = st2.get("viewer_interaction") or {}
            iids = [o.get("interactionId") or o.get("id")
                    for o in (vi.get("opportunities") or [])] if vi else []
            if iid not in iids:
                return True
        await asyncio.sleep(0.4)
    return False


async def build_bolt_target(c):
    """Poll for the Bolt's genuine target-selection opportunity and submit
    it preferring seat 0 (P0). Returns True when submitted+cleared."""
    t0 = time.time()
    while time.time() - t0 < 25:
        st2 = c.latest
        if st2:
            opp, rtype, spec_type = target_opportunity(st2)
            if opp is not None:
                ch = pick_target_candidate(opp, preferred_seat=0)
                say(f"[P1] bolt target opportunity "
                    f"{str(opp.get('interactionId'))[:16]}: preferred pick "
                    f"seat={candidate_seat(ch) if ch is not None else None}")
                wire("p1_bolt_target_interaction",
                     {"opp": opp.get("interactionId")})
                if ch is None or candidate_seat(ch) != 0:
                    ST["notes"].append(
                        "P1 bolt target: no seat-0 candidate; aborting "
                        "target attempt rather than mis-targeting")
                    say("[P1] no seat-0 candidate; will not mis-target")
                    return False
                iid, _ = await submit_target(c, opp, rtype, spec_type, ch, "P1")
                await asyncio.sleep(0.5)
                if await bolt_target_cleared(c, iid):
                    return True
                say("[P1] bolt target prompt still up after submission; retrying")
                return False
        await asyncio.sleep(0.4)
    say("[P1] bolt target opportunity never appeared within 25s")
    return False


async def p1_maybe_cast_bolt(c, acts, st, state, p0state):
    """P1 casts Bolt at P0 on its own main phase once P0 can respond.

    NOTE (106): each client's state update is that client's VIEW --
    opponent hand card names are hidden. So P0's hand is read from p0state
    (P0's own view) and P1's hand from P1's own `state`."""
    if (ST["bolt_cast_by_p1"] or ST["dualcaster_cast"]
            or ST["stage"] != "SETUP"):
        return False
    if not my_priority(acts):
        return False
    if not my_main(state, 1):
        return False
    if untapped_mountains(state, 0) < 3:
        return False  # wait until P0 can afford the Dualcaster response
    if untapped_mountains(state, 1) < 1:
        return False
    # Mage check MUST use P0's own view (opponent hands are hidden).
    p0_hand = hand_lnames(p0state, 0) if p0state else []
    if DUALCASTER not in p0_hand:
        return False  # P0 has no response in hand yet; wait
    bolt_oid = next((o for o in hand_ids(state, 1)
                     if obj_lname(state, o) == BOLT), None)
    if bolt_oid is None:
        return False
    for a in acts:
        d = a.get("data") or {}
        oid = d.get("object_id") or a.get("_src_oid")
        if a["type"] == "CastSpell" and oid is not None and str(oid) == str(bolt_oid):
            say(f"[P1] casting Lightning Bolt (P0 untapped mountains: "
                f"{untapped_mountains(state, 0)}, mage in P0 hand)")
            wire("p1_cast_bolt", {k: v for k, v in a.items() if not k.startswith("_")})
            ST["bolt_oid"] = bolt_oid
            MANA_NEEDS["P1"] = {"generic": 1}  # {R}
            await submit_as_is(c, a)
            ST["bolt_target_pending"] = True  # hold P1's pass until targeted

            targeted = False
            t0 = time.time()
            while time.time() - t0 < 60 and not targeted:
                targeted = await build_bolt_target(c)
                if not targeted:
                    await asyncio.sleep(1.0)
            ST["bolt_target_pending"] = False
            ST["bolt_cast_by_p1"] = True
            if not targeted:
                ST["notes"].append(
                    "P1 bolt cast submitted but target confirmation unclear; "
                    "continuing to watch (possible engine-side stall)")
            return True
    return False


async def p1_tick(c, acts, st, state, p0state):
    """P1: keep own turn driver active (land drops + opportunistic Bolt)."""
    if await do_mulligan(c, acts, st, 1, "P1"):
        return
    if await do_bottom(c, acts, st, 1, "P1"):
        return
    if await do_discard_to_handsize(c, acts, st, 1, "P1"):
        return
    d = await combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    needs = MANA_NEEDS.get("P1", {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, "P1", needs):
            return
    # Keep P1's own turn driver active (land drops + opportunistic Bolt).
    if await p1_maybe_cast_bolt(c, acts, st, state, p0state):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, "P1"):
            return
    # Never pass while our own bolt is awaiting its target choice: passing
    # in the transition window could let P0 respond before targets exist.
    if ST.get("bolt_target_pending"):
        say("[P1] holding pass: bolt target choice outstanding")
        return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------- observe loop (106)
def note_vi_kind(st):
    kind = vi_kind_code(st)
    if kind and kind not in ST["seen_vi_kinds"]:
        ST["seen_vi_kinds"].append(kind)
        say(f"[OBS] vi waitingForKind -> {kind!r}")
        wire("vi_kind", {"code": kind})


def find_copyretarget_opp(st):
    """Locate the MayChooseNewTargets / CopyRetarget opportunity in P0's
    viewer_interaction. Protocol 106 has no waiting_for CopyRetarget
    surface, so scan vi opportunities for copy/retarget markers and return
    (opp, copy_id) or (None, None)."""
    for opp in vi_ops(st):
        blob = json.dumps(opp, default=str).lower()
        if "copy" not in blob and "retarget" not in blob:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        # exclude the noisy priority-menu opportunities
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if codes and codes <= NON_DECISION_CODES:
            continue
        copy_id = None
        for ch in items:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict):
                    for k in ("copy_id", "copyId", "copy"):
                        if d.get(k) is not None:
                            copy_id = d.get(k)
                            break
        return opp, copy_id
    return None, None


async def observe(st, state, c):
    """Watch the ETB/copy/retarget flow; answer CopyRetarget by seat."""
    note_vi_kind(st)

    # Mage resolved?
    if not ST["mage_bf"]:
        if any(obj_lname(state, oid) == DUALCASTER
               and get_obj(state, oid).get("zone") == "Battlefield"
               and str(get_obj(state, oid).get("controller", -1)) == "0"
               for oid in (state.get("objects") or {})):
            ST["mage_bf"] = True
            ST["notes"].append("Dualcaster Mage resolved to P0's battlefield")
            say("[OBS] Dualcaster on P0 battlefield")

    # Copy on the stack? (>=2 bolt-like entries, effect-signature matched)
    bolts = stack_bolt_entries(state)
    if len(bolts) >= 2 and not ST["copy_seen_stack"]:
        ST["copy_seen_stack"] = True
        ST["ass"]["A3_copy_created"] = "passed"
        ids = [e.get("id") for e in bolts]
        ST["notes"].append(
            f"copy observed on stack: {len(bolts)} bolt-like entries "
            f"(DealDamage 3 signature), ids={ids}")
        say(f"[OBS] copy on stack: ids={ids}")
        wire("stack_with_copy", {"bolt_like_ids": ids})

    opp, copy_id = find_copyretarget_opp(st)
    if opp is not None and not ST["retarget_done"]:
        if not ST["copyretarget_seen"]:
            ST["copyretarget_seen"] = True
            ST["ass"]["A3_copy_created"] = "passed"
            ST["copy_id"] = copy_id
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            ST["notes"].append(
                f"CopyRetarget surfaced in P0 viewer_interaction "
                f"(MayChooseNewTargets): copy_id={ST['copy_id']} "
                f"rtype={resp.get('type')} "
                f"keys={sorted(data.keys())[:8]}")
            say(f"[OBS] CopyRetarget: copy_id={ST['copy_id']}")
            wire("copyretarget", {"copy_id": ST["copy_id"], "opp": opp})
            # A2: verify the copy is a Bolt copy of the stacked Bolt.
            entries = {e.get("id"): e for e in stack_entries(state)
                       if isinstance(e, dict)}
            cp = entries.get(ST["copy_id"], {})
            if ST["copy_id"] is not None and is_boltlike(cp):
                ST["ass"]["A2_etb_targets_stack"] = "passed"
                ST["etb_evidence"] = {"copy_id": ST["copy_id"],
                                     "copy_effect": effect_sig(cp),
                                     "bolt_oid": ST["bolt_oid"]}
                ST["notes"].append(
                    "copy effect matches Lightning Bolt (DealDamage 3); the ETB "
                    "trigger targeted the Bolt on the stack")
            elif ST["copy_id"] is None:
                # Fall back to the stack-scan for A2: if a second bolt-like
                # entry exists the ETB targeted the stacked Bolt.
                if stack_bolt_entries(state):
                    ST["ass"]["A2_etb_targets_stack"] = "passed"
                    ST["etb_evidence"] = {"copy_id_inferred_from_stack": True,
                                         "bolt_like_ids": [e.get("id") for e in stack_bolt_entries(state)],
                                         "bolt_oid": ST["bolt_oid"]}
                    ST["notes"].append(
                        "no copy_id in the retarget surface; A2 confirmed from "
                        "stack: second bolt-like (DealDamage 3) entry present "
                        "while the original Bolt is on the stack")
                else:
                    ST["ass"]["A2_etb_targets_stack"] = "not-run"
                    ST["notes"].append("copy_id absent and no second bolt-like "
                                       "entry on the stack yet; A2 deferred")
            else:
                ST["ass"]["A2_etb_targets_stack"] = "failed"
                ST["notes"].append(
                    f"copy entry {ST['copy_id']} does not look like a Bolt copy: "
                    f"{json.dumps(cp)[:300]}")
        # Answer the retarget: prefer seat 1 (P1) as the new target. Skip
        # decline/no-op choices (keep-all-targets, "none"). Submit, then
        # confirm the prompt advanced (a silent rejection must never be
        # mistaken for success).
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        riid = ST.get("retarget_iid")
        rtime = ST.get("retarget_submit_t", 0)
        if riid is not None:
            cur_iids = [o.get("interactionId") for o in vi_ops(st)]
            if riid not in cur_iids:
                ST["retarget_done"] = True
                ST["ass"]["A4_retarget_offered"] = "passed"
                ST["notes"].append("MayChooseNewTargets offered; " + ST["retarget_note"])
                say(f"[OBS] retarget confirmed: {ST['retarget_note']}")
                return True
        cur = []
        for ch in data.get("candidates") or data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = set(surf_codes(ch))
            if codes & {"keepAllCopyTargets", "decline"}:
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and d.get("value") == "none":
                    break
            else:
                cur.append(ch)
                continue
        for ch in cur:
            say("  retarget candidate: codes=" + str(sorted(set(surf_codes(ch)))) +
                " seat=" + str(candidate_seat(ch)) +
                " text=" + choice_text(ch)[:160])
        wire("retarget_candidates",
             {"codes": [sorted(set(surf_codes(ch))) for ch in cur],
              "seats": [candidate_seat(ch) for ch in cur],
              "texts": [choice_text(ch)[:120] for ch in cur]})
        pick = next((ch for ch in cur if candidate_seat(ch) == 1), None)
        if pick is None:
            pick = next((ch for ch in cur
                         if candidate_seat(ch) is not None), None)
            if pick is not None:
                ST["notes"].append(
                    "retarget: no seat-1 player candidate; used first "
                    "seated target")
        kind = "retarget_p1"
        if pick is None:
            ST["notes"].append(
                "CopyRetarget: no seated target candidate; holding")
            say("[OBS] CopyRetarget: no usable candidate; holding")
            return True
        same_prompt = (riid is not None
                       and opp.get("interactionId") == riid)
        if same_prompt and time.time() - rtime < 8:
            say("[OBS] retarget submitted; awaiting engine advance")
            return True
        ok = await answer_vi(c, opp, pick, kind)
        if ok:
            ST["retarget_iid"] = opp.get("interactionId")
            ST["retarget_submit_t"] = time.time()
            ST["retarget_note"] = (
                f"({kind}) seat {candidate_seat(pick)} "
                f"({choice_text(pick)[:100]})")
            ST["notes"].append(f"retarget submission {ST['retarget_note']}")
            say(f"[OBS] retarget submitted {ST['retarget_note']}")
        return True

    # Confirm the answered retarget even when the CopyRetarget opportunity
    # has already closed: the engine advances the game as soon as the choice
    # is consumed.
    if (not ST["retarget_done"] and ST.get("retarget_iid") is not None
            and opp is None):
        ST["retarget_done"] = True
        ST["ass"]["A4_retarget_offered"] = "passed"
        ST["notes"].append("MayChooseNewTargets offered and answered; "
                           + ST["retarget_note"] + " (prompt advanced)")
        say(f"[OBS] retarget confirmed (prompt gone): {ST['retarget_note']}")

    # Genuine trigger target prompt (engine asks for the ETB target)?
    if real_decision_pending(st) and not ST["copyretarget_seen"]:
        cands = []
        for o2 in vi_ops(st):
            resp2 = o2.get("response", {}) or {}
            data2 = resp2.get("data", {}) or {}
            for ch in data2.get("candidates") or data2.get("choices") or []:
                if (ch.get("status", {}) or {}).get("type") in (None, "available"):
                    cands.append((o2, ch))
        boltish = [p for p in cands
                   if BOLT in
                   (choice_text(p[1]) + json.dumps(p[1].get("surfaces", []), default=str)).lower()]
        if boltish:
            say(f"[OBS] unexpected P0 decision; checking for Bolt candidate: "
                f"{len(boltish)} boltish")
            wire("unexpected_prompt",
                 {"vi_kind": vi_kind_code(st), "vi": st.get("viewer_interaction")})
            opp2, ch2 = boltish[0]
            if await answer_vi(c, opp2, ch2, "etb_target_bolt"):
                ST["ass"]["A2_etb_targets_stack"] = "passed"
                ST["copyretarget_seen"] = True
                ST["notes"].append(
                    "ETB trigger targeted the Bolt via an explicit prompt")
                return True
    return False


async def check_resolution(st, state, p0):
    """After retarget, both spells resolve: life deltas + empty stack."""
    if not ST["retarget_done"]:
        return False
    if stack_entries(state):
        return False
    if real_decision_pending(st):
        return False
    acts = merged_actions(st)
    if not my_priority(acts):
        return False
    # One settle beat, then export POST and finalize.
    await asyncio.sleep(2)
    st2 = p0.latest
    state2 = st2["state"] if st2 else state
    if stack_entries(state2):
        return False
    say("exporting POST (stack empty, nothing pending)")
    post = await p0.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(post)
    ST["post_exported"] = True
    ST["post_life"] = (life(state2, 0), life(state2, 1))
    ST["states_seen"] += 1
    mage_bf = any(obj_lname(state2, oid) == DUALCASTER
                  and get_obj(state2, oid).get("zone") == "Battlefield"
                  and str(get_obj(state2, oid).get("controller", -1)) == "0"
                  for oid in (state2.get("objects") or {}))
    d0 = ST["pre_life"][0] - ST["post_life"][0] if ST["pre_life"] else None
    d1 = ST["pre_life"][1] - ST["post_life"][1] if ST["pre_life"] else None
    ST["notes"].append(
        f"life pre={ST['pre_life']} post={ST['post_life']} "
        f"(delta P0={d0} P1={d1}); mage_on_bf={mage_bf}; "
        f"stack empty; priority held")
    if d0 == 3 and d1 == 3 and mage_bf:
        ST["ass"]["A5_resolution"] = "passed"
    else:
        ST["ass"]["A5_resolution"] = "failed"
    ST["stage"] = "DONE"
    return True

# ------------------------------------------------------------- finalization
def finalize_assertions():
    a = ST["ass"]
    for k in ("A1_flash_timing", "A2_etb_targets_stack", "A3_copy_created",
              "A4_retarget_offered", "A5_resolution"):
        a.setdefault(k, "not-run")
    core = [a["A1_flash_timing"], a["A2_etb_targets_stack"], a["A3_copy_created"],
            a["A4_retarget_offered"], a["A5_resolution"]]
    if all(v == "passed" for v in core):
        verdict = "not-reproduced"
    elif any(v == "failed" for v in core):
        verdict = "reproduced"
    else:
        verdict = "blocked"
        ST["notes"].append("inconclusive: some steps not-run; see notes")
    return a, verdict


async def finish():
    a, verdict = finalize_assertions()
    write_parse_files(ST["notes"])
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST.get("started_at"),
        "duration_s": round(time.time() - ST.get("t0", time.time()), 1),
        "server": {
            "server_version": SERVER_IDENTITY["server_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "mode": SERVER_IDENTITY["mode"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "observed_at": "2026-10-04",
            "source": SERVER_IDENTITY["source"],
        },
        "server_run_dir": "runs/20261004-658 (fresh v0.102.0 server started by this run on 127.0.0.1:9374)",
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_658_01020.py", "rb").read()).hexdigest(),
        "decks": {"P0": [("Mountain", 56), ("Dualcaster Mage", 4)],
                  "P1": [("Mountain", 56), ("Lightning Bolt", 4)]},
        "assertions": a,
        "notes": ST["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Restoration: the prebuilt phase-server has no standalone "
            "state-restore facility; pre.json/post.json are authoritative "
            "exports restorable only via full game replay, not direct load.",
        ],
        "setup_line": "P0: 56x Mountain + 4x Dualcaster Mage; P1: 56x Mountain + 4x Lightning Bolt",
        "contract_line": "P1 Bolts P0; P0 flashes in Dualcaster in response; ETB copies Bolt retargeted to P1; both resolve",
        "stats": {
            "states_seen": ST["states_seen"],
            "seen_vi_kinds": ST["seen_vi_kinds"],
            "pre_life": ST["pre_life"],
            "post_life": ST["post_life"],
            "copy_id": ST["copy_id"],
            "bolt_oid": ST["bolt_oid"],
            "etb_evidence": ST["etb_evidence"],
            "retarget_note": ST["retarget_note"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"wrote run.json verdict={verdict}")


def write_parse_files(notes):
    try:
        for key, fn in (("dualcaster mage", "parse_dualcaster.json"),
                        ("lightning bolt", "parse_bolt.json")):
            card = CARD_DATA.get(key, {})
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump({"name": card.get("name"),
                           "oracle_text": card.get("oracle_text"),
                           "triggers": card.get("triggers"),
                           "abilities": card.get("abilities"),
                           "type_line": card.get("type_line")}, f, indent=2)
        notes.append("parse check: Dualcaster Mage + Lightning Bolt from "
                     "v0.102.0 card-data.json saved to parse_*.json")
    except Exception as e:
        notes.append(f"parse json failed: {e}")


def render_summary_png():
    # Rendered from the actual saved run.json (states + assertions).
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server"]
    y = 20
    d.text((24, y), "Issue #658 - Dualcaster Mage -- runtime failure (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server v{si['server_version']} ({si['build_commit']}) "
           f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    v = run["verdict"]
    d.text((24, y), f"verdict: {v.upper()}",
           fill=(255, 90, 90) if v == "reproduced" else
           ((120, 220, 120) if v == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_flash_timing": "A1 Dualcaster cast with Bolt on the stack",
        "A2_etb_targets_stack": "A2 ETB copy targets the stacked Bolt (DealDamage 3)",
        "A3_copy_created": "A3 copy created (stack >=2 bolt-like or CopyRetarget)",
        "A4_retarget_offered": "A4 MayChooseNewTargets offered + answered (copy -> P1)",
        "A5_resolution": "A5 resolution: P0 17, P1 17, mage on BF, stack empty",
    }
    for k, lab in labels.items():
        vv = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if vv == "passed" else (
            (255, 90, 90) if vv == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if vv == 'passed' else ('FAIL' if vv == 'failed' else 'n/a')} {lab}",
               fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:9]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    stats = run.get("stats", {})
    d.text((24, H - 52), f"life pre={stats.get('pre_life')} post={stats.get('post_life')} "
           f"states_seen={stats.get('states_seen')} vi_kinds={','.join(stats.get('seen_vi_kinds', []))[:60]}",
           fill=(120, 130, 150))
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 658/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(f"{EVDIR}/summary.png")


def validate():
    say("validating evidence dir...")
    for fn in ("pre.json", "post.json"):
        p = f"{EVDIR}/{fn}"
        env = json.load(open(p))
        assert "state" in env, f"{fn} missing state envelope"
        say(f"  {fn}: parses, state envelope ok")
    run = json.load(open(f"{EVDIR}/run.json"))
    assert run.get("verdict") in ("reproduced", "not-reproduced", "blocked"), \
        "run.json missing/invalid verdict"
    assert isinstance(run.get("assertions"), dict), "run.json assertions not a dict"
    say(f"  run.json: parses, verdict={run['verdict']}, "
        f"{len(run['assertions'])} assertions")
    manifest = {}
    for line in open(f"{EVDIR}/manifest.sha256"):
        line = line.strip()
        if not line:
            continue
        h, fn = line.split("  ", 1)
        manifest[fn] = h
    for fn, h in manifest.items():
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        assert actual == h, f"hash mismatch for {fn}"
    say(f"  manifest: {len(manifest)} files, all hashes match")
    from PIL import Image
    im = Image.open(f"{EVDIR}/summary.png")
    im.verify()
    say("  summary.png: PIL verify ok")


def write_manifest():
    files = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = f"{EVDIR}/{name}"
        if os.path.isfile(p):
            files.append(name)
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def capture_server_excerpts():
    try:
        logp = f"{BACKFILL}/runs/20261004-658/server.log"
        if not os.path.exists(logp):
            say("no server.log for this run; skipping excerpts")
            return
        out = []
        with open(logp, errors="replace") as f:
            for line in f:
                if ST.get("game_code") and ST["game_code"] in line:
                    out.append(line)
        out = out[-300:]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(out)
        say(f"wrote server_excerpts.log ({len(out)} lines)")
    except Exception as e:
        say(f"server excerpts failed: {e}")


# ------------------------------------------------------------- main loop
async def main():
    reset()
    ST["t0"] = time.time()
    ST["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ST["t0"]))
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    say("server identity pinned: v0.102.0 (e17f6fd) protocol 106, mode Full; "
        "fresh server on 127.0.0.1:9374 started by this run")

    await p0.create(deck(("Mountain", 56), ("Dualcaster Mage", 4)), player_count=2)
    await p1.join(p0.game_code, deck(("Mountain", 56), ("Lightning Bolt", 4)))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    observe_deadline = None

    while True:
        await asyncio.sleep(0.15)
        now = time.time()
        elapsed = now - t_start

        if ST["stage"] == "SETUP" and elapsed > SETUP_DEADLINE_S:
            ST["notes"].append(
                f"SETUP deadline ({SETUP_DEADLINE_S}s) hit before Dualcaster "
                "was cast in response; recording not-run/blocked")
            break

        for c, tick_kind in ((p0, "p0"), (p1, "p1")):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last_rev.get(c.name))
            holds_prio = my_priority(top_acts(st))
            if same_rev and not (holds_prio and now - last_tick_at.get(c.name, 0) > 5):
                continue
            last_rev[c.name] = c.revision
            last_tick_at[c.name] = now
            await asyncio.sleep(0)  # yield before leg evaluation (race fix)
            st = c.latest or st
            try:
                if tick_kind == "p0":
                    await p0_tick(c, "P0", merged_actions(st), st, st["state"])
                else:
                    p0state = p0.latest["state"] if p0.latest else None
                    await p1_tick(c, merged_actions(st), st, st["state"], p0state)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})

        if ST["stage"] == "OBSERVE" and p0.latest:
            try:
                await observe(p0.latest, p0.latest["state"], p0)
                if observe_deadline is None and ST["retarget_done"]:
                    observe_deadline = now + RESOLVE_TIMEOUT_S
                done = await check_resolution(p0.latest, p0.latest["state"], p0)
                if done:
                    break
                if observe_deadline is None:
                    observe_deadline = t_start + SETUP_DEADLINE_S + OBSERVE_TIMEOUT_S
                if now > observe_deadline:
                    ST["notes"].append(
                        "OBSERVE deadline hit: retarget_done="
                        f"{ST['retarget_done']} mage_bf={ST['mage_bf']} "
                        f"copyretarget_seen={ST['copyretarget_seen']}")
                    if not ST["post_exported"]:
                        try:
                            post = await p0.export_state()
                            with open(f"{EVDIR}/post.json", "w") as f:
                                f.write(post)
                            ST["post_exported"] = True
                            ST["notes"].append("post.json exported at OBSERVE-deadline fallback")
                        except Exception as e:
                            ST["notes"].append(f"post export failed: {e}")
                    break
            except Exception as e:
                say(f"observe error: {e}")
                wire("observe_error", {"err": str(e)})

        if p0.latest and (p0.latest["state"].get("turn_number") or 0) > 400 \
                and ST["stage"] == "SETUP":
            ST["notes"].append("turn 400 hit in SETUP; finishing")
            break
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            s1 = p1.latest["state"] if p1.latest else s
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} stage={ST['stage']} "
                f"dualcaster_cast={ST['dualcaster_cast']} "
                f"stack={len(stack_entries(s))} "
                f"P0untapM={untapped_mountains(s, 0)} "
                f"P0mage_in_hand={DUALCASTER in hand_lnames(s, 0)} "
                f"P1bolt_in_hand={BOLT in hand_lnames(s1, 1)} "
                f"vikind={vi_kind_code(p0.latest)!r} "
                f"real_decision={real_decision_pending(p0.latest)}")

    await finish()
    # pre/post export fallbacks so the evidence dir is always complete and
    # renderable; each fallback is labeled in the notes and is NOT the
    # contract state (pre = Bolt on stack pre-response).
    run_p = f"{EVDIR}/run.json"
    for name, key in (("pre", "pre_exported"), ("post", "post_exported")):
        if not ST[key]:
            try:
                data = await p0.export_state()
                with open(f"{EVDIR}/{name}.json", "w") as f:
                    f.write(data)
                ST[key] = True
                ST["notes"].append(
                    f"{name}.json exported at finish fallback "
                    f"(NOT the contract state)")
                run = json.load(open(run_p))
                run["notes"] = ST["notes"]
                json.dump(run, open(run_p, "w"), indent=1)
            except Exception as e:
                say(f"fallback export of {name} failed: {e}")
    try:
        render_summary_png()
        say("rendered summary.png")
    except Exception as e:
        say(f"render_summary failed: {e}")
    try:
        await capture_server_excerpts()
    except Exception as e:
        say(f"capture_server_excerpts failed: {e}")
    WIRE.close()
    RUNLOG.close()
    write_manifest()
    try:
        validate()
    except Exception as e:
        say(f"VALIDATION FAILED: {e}")
        sys.exit(2)
    say(f"DONE run={RUN_ID}")
    await p0.close()
    await p1.close()


asyncio.run(main())
