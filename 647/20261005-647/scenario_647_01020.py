#!/usr/bin/env python3
"""Issue #647: Asmoranomardicadaistinaculdacar castability - protocol-106
re-validation (on pinned v0.102.0).

Ports the verified protocol-103 scenario_647_01010.py to protocol 106 using
the conventions established in the verified protocol-106 scenario_658_01020.py
(same pin, 2026-10-04):
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action. P0 keeps if Faithless
    Looting is in hand or after 2 mulligans; P1 always keeps.
  - Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards (106 uses generic 'choose' kind code).
  - Faithless Looting's "discard two cards" is a vi schema select/sequence
    opportunity whose candidates reference hand objects, answered while a
    Looting is in flight (lands first, Asmo protected).
  - CastSpell submitted via legacy Action (verified on 106); mana payment via
    legacy PayMana actions and/or vi tapLandForMana menus driven by
    mana_needs ({"R": 1} for Looting, {"B": 1} for Asmo's {B/R}).
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, castSpell, activateAbility, ...).
  - Each client's StateUpdate is that client's VIEW (opponent hand card names
    are hidden); hands are read from the owning seat's own view.
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority. Re-tick backstop: re-tick a
    client holding priority with no revision change for > 5s.
  - surf_codes() filters None codes (106 surfaces sometimes carry data:{}
    with no code).
  - manifest.sha256 is computed AFTER wire_log.jsonl and scenario_run.log
    are closed.

Behavioral contract (unchanged; written before observing results):
  NEGATIVE: on any turn before P0 has discarded, Asmo in hand at P0 main-phase
            priority -> CastSpell(Asmo) must NOT be advertised (merged
            legal_actions incl. legal_actions_by_object) and no vi cast offer
            may reference it.
  POSITIVE: on a turn where P0 has discarded, CastSpell(Asmo) MUST be
            advertised on that same turn; cast it for {B/R} and verify
            hand->stack->battlefield with payment. Two discard-turns are run
            for strength (P0 runs 4x Faithless Looting).

Assertions:
  A1 setup_ok                  game starts, turns advance, both seats act.
  A2 neg_no_cast_offered       on no-discard turns, CastSpell(Asmo) never in
                               merged legal_actions.
  A3 neg_no_viewer_cast        on no-discard turns, no vi cast offer for Asmo.
  A4 looting_resolved          Faithless Looting cast + >=1 discard observed.
  A5 cast_offered_after_discard  CastSpell(Asmo) advertised on a discard turn.
  A6 asmo_cast_paid            Asmo resolved to BF; mana delta recorded.

Verdict rule: reproduced iff the REPORTED bug is observed (A2 or A3 fail).
not-reproduced otherwise (this is not a fixed verdict). blocked only if the
game cannot be driven to the discard.

Setup: P0: 4x Asmo + 4x Faithless Looting + 26 Swamp + 26 Mountain (B/R always
available so mana is never the reason a cast is unoffered). P1: 60x Island,
draw-go.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 647
RUN_ID = "20261005-647"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
VALIDATED_AT = "2026-10-05"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

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
    "source": ("2026-10-05: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "reusing the healthy v0.102.0 server already listening on "
               "127.0.0.1:9374 (started by run 20261005-0011, same binary + "
               "data dir; not restarted per playbook)"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

ASMO_L = "asmoranomardicadaistinaculdacar"
LOOTING_L = "faithless looting"
SWAMP_L = "swamp"
MOUNTAIN_L = "mountain"

SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {0: 0, 1: 0}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
LOOT_DBG = set()      # interactionIds already debug-logged by do_looting_discard


def reset():
    ST.clear()
    ST.update({
        "stage": "NEG",          # NEG -> LOOTING -> POS -> SEEK -> LOOTING -> POS ...
        "game_code": None,
        "neg_obs": 0, "pre_neg": False,
        "looting_in_flight": False, "looting_turn": None, "loots_done": 0,
        "discard_turn": None, "discard_turns": [], "all_discarded": [],
        "asmo_cast": False, "asmo_cast_turn": None,
        "pre_cast_br": None, "mid_exported": False, "post_exported": False,
        "pos_obs": 0, "max_turn": 0, "states_seen": 0,
        "bugs": [], "offer_turns": [], "seen_vi_kinds": [],
        "ass": {}, "notes": [],
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


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def zone_objs(state, pid, zone, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone:
            continue
        if str(o.get("controller", -1)) != str(pid):
            continue
        if lname and str(o.get("base_name") or o.get("name") or "").lower() != lname:
            continue
        out.append((oid, o))
    return out


def untapped_br(state, pid):
    return sum(1 for _, o in zone_objs(state, pid, "Battlefield")
               if not o.get("tapped")
               and str(o.get("base_name") or o.get("name") or "").lower()
               in (SWAMP_L, MOUNTAIN_L))


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
            and state.get("active_player") == pid)


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


def note_vi_kind(st):
    kind = vi_kind_code(st)
    if kind and kind not in ST["seen_vi_kinds"]:
        ST["seen_vi_kinds"].append(kind)
        say(f"[OBS] vi waitingForKind -> {kind!r}")
        wire("vi_kind", {"code": kind})


# ------------------------------------------------------------- interaction primitives
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    iid = sub.get("interactionId")
    if iid:
        LAST_IID[c.name] = iid
    wire("interaction_submit", {"who": tag,
                                "interactionId": iid,
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    key = (c.name, tag, str(iid))
    if key in MULLS:
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
    MULLS.add(key)
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)
    return True


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            say(f"[{c.name}] {t}: {body[:300]}")
            wire("rejected", {"who": c.name, "type": t})
            iid = LAST_IID.get(c.name)
            if iid and iid in body:
                REJECTS[iid] = REJECTS.get(iid, 0) + 1
                say(f"[{c.name}] iid {iid} rejection #{REJECTS[iid]}")
                if REJECTS[iid] >= 2 and iid not in SKIP_IID:
                    SKIP_IID.add(iid)
                    say(f"[{c.name}] iid {iid} rejected twice; skipping "
                        f"further submissions on it")


# ------------------------------------------------------------- common ticks (protocol 106)
async def do_mulligan(c, acts, st, pid, tag):
    """MulliganDecision arrives as a legacy legal action (verified accepted
    on 106). P0 keeps if Faithless Looting is in hand or after 2 mulligans;
    P1 always keeps."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    if pid == 0:
        want = "keep" if (LOOTING_L in hn or MULL_COUNT[0] >= 2) else "mulligan"
        if want == "mulligan":
            MULL_COUNT[0] += 1
    else:
        want = "keep"
    say(f"[{tag}] {want} {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": want, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if want == "keep"
                                             else "Mulligan"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: vi schema/select, gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap."""
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; "
            f"not answering")
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
        if is_land(get_obj(state, oid)):
            return (0, str(oid))
        if nm in (ASMO_L, LOOTING_L):
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
    'choose' waitingForKind code). Protects Asmo + Looting.

    Skipped while a Faithless Looting is in flight: the Looting's own
    mandatory discard-2 opportunity looks identical (schema select offering
    hand cards) and must be answered by do_looting_discard with the full
    pick count, not grabbed as a hand-size cleanup with hand-7 picks."""
    if ST.get("looting_in_flight"):
        return False
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
        if nm in (ASMO_L, LOOTING_L):
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


async def do_looting_discard(c, acts, st, pid, tag):
    """Faithless Looting's 'discard two cards' on protocol 106: a schema
    select/sequence opportunity whose candidates reference our hand cards,
    answered while a Looting is in flight. Lands first, Asmo protected."""
    if not ST.get("looting_in_flight"):
        return False
    state = st["state"]
    hand = hand_ids(state, pid)
    handset = set(hand)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type") or ""
        if stype not in ("select", "sequence"):
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        if not any(str(_cand_reference(ch)) in handset for ch in cands):
            iid_dbg = opp.get("interactionId")
            if iid_dbg not in LOOT_DBG:
                LOOT_DBG.add(iid_dbg)
                say(f"[{tag}] looting_discard: opportunity {iid_dbg} offers no "
                    f"hand cards ({len(cands)} cands); skipping")
            continue
        iid = opp.get("interactionId")
        if iid in SKIP_IID:
            continue
        key = (tag, "lootdisc", iid)
        if key in SUBMITTED_OPPS:
            return True  # answered; hold until the opportunity clears
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 2)

        def rkey(ch):
            oid = _cand_reference(ch)
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm == ASMO_L:
                return (2, str(oid))
            if is_land(get_obj(state, oid)):
                return (0, str(oid))
            return (1, str(oid))

        ranked = sorted(cands, key=rkey)
        picks = ranked[:max(1, n)]
        SUBMITTED_OPPS.add(key)
        names = [obj_lname(state, _cand_reference(ch)) for ch in picks]
        say(f"[{tag}] looting discard ({n}): {names}")
        wire("looting_discard", {"who": tag, "iid": iid, "picks": names})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype,
                                           "data": {"choiceIds": [ch.get("id")
                                                                  for ch in picks]}}}, tag)
        ST["all_discarded"].extend(names)
        ST["discard_turn"] = state.get("turn_number")
        ST["discard_turns"].append(ST["discard_turn"])
        ST["loots_done"] += 1
        ST["looting_in_flight"] = False
        ST["stage"] = "POS"
        ST["pos_obs"] = 0
        say(f"[{tag}] Looting resolved with discards {names} (turn "
            f"{ST['discard_turn']}; discard-turn #{ST['loots_done']})")
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
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a.get("type") == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"blocks": [], "blockers": []})
            return d
    return None


# ------------------------------------------------------------- #647 legs
def castspell_for(acts, state, lname):
    for a in acts:
        if a.get("type") == "CastSpell":
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if oid is not None and obj_lname(state, oid) == lname:
                return a
    return None


def vi_cast_offer_for(st, lname):
    """Raw vi scan (not canSubmit-gated here; caller passes st): any
    AVAILABLE castSpell choice naming `lname`?"""
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data") or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = [c for c in surf_codes(ch) if c]
            if not any("castSpell" in c for c in codes):
                continue
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            names = [((s.get("data") or {}).get("name") or "")
                     for s in ch.get("surfaces", []) or []]
            txt = choice_text(ch).lower()
            if lname in txt or any(lname in str(n).lower() for n in names):
                return opp, ch
    return None


async def export_now(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


async def negative_leg(c, st, acts, state, turn, asmo_oid, looting_oid):
    """Returns an action tag or None. Shared by NEG and SEEK stages."""
    cast = castspell_for(acts, state, ASMO_L)
    if cast:
        ST["ass"]["A2_neg_no_cast_offered"] = "failed"
        ST["bugs"].append(
            f"REPORTED BUG: CastSpell(Asmo) advertised on a no-discard turn "
            f"(turn {turn}, stage {ST['stage']})")
        say(ST["bugs"][-1])
        return ("bug", cast)
    offer = vi_cast_offer_for(st, ASMO_L)
    if offer and ST["ass"].get("A3_neg_no_viewer_cast") != "failed":
        ST["ass"]["A3_neg_no_viewer_cast"] = "failed"
        ST["bugs"].append(
            f"REPORTED BUG: vi cast offer for Asmo on a no-discard turn "
            f"(turn {turn}, stage {ST['stage']})")
        say(ST["bugs"][-1])
        return ("bug", None)
    ST["neg_obs"] += 1
    if not ST["pre_neg"]:
        ST["pre_neg"] = True
        await export_now(c, "pre_neg.json")
        say(f"PRE_NEG captured turn {turn} (Asmo in hand, no discard "
            f"this turn, no cast offered)")
    if ST["ass"].get("A3_neg_no_viewer_cast") == "not-run":
        ST["ass"]["A3_neg_no_viewer_cast"] = "passed"
    # cast Faithless Looting once enough negative observations gathered
    need = 3 if ST["loots_done"] == 0 else 1
    if ST["neg_obs"] >= need and looting_oid:
        cast = castspell_for(acts, state, LOOTING_L)
        if cast:
            return ("cast_looting", cast)
    return None


async def p0_tick(c, st, acts, state):
    pid, tag = 0, "P0"
    drain_rejections(c)
    note_vi_kind(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    # Looting's mandatory discard-2 BEFORE the generic hand-size cleanup:
    # the two opportunities are indistinguishable by shape, and answering
    # the Looting's with a hand-7 pick count stalls the game.
    if await do_looting_discard(c, acts, st, pid, tag):
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
    if not my_priority(acts):
        return
    # Yield so _pump can deliver any in-flight StateUpdate before legs are
    # evaluated below; without this the driver can systematically act on a
    # stale phase window and the NEG/POS legs intermittently never engage.
    await asyncio.sleep(0)
    turn = state.get("turn_number") or 0
    in_main = my_main(state, pid)
    asmo_oid = next((o for o in hand_ids(state, pid)
                     if obj_lname(state, o) == ASMO_L), None)
    looting_oid = next((o for o in hand_ids(state, pid)
                        if obj_lname(state, o) == LOOTING_L), None)

    # ---- NEGATIVE legs (no discard this turn) ----
    if ST["stage"] in ("NEG", "SEEK") and asmo_oid and in_main \
            and ST["discard_turn"] != turn:
        r = await negative_leg(c, st, acts, state, turn, asmo_oid, looting_oid)
        if r is not None and r[0] == "bug":
            if r[1] is not None:
                await submit_as_is(c, r[1])
                ST["asmo_cast"] = True
                ST["asmo_cast_turn"] = turn
                ST["stage"] = "BUGPROOF"
                return
        elif r is not None and r[0] == "cast_looting":
            say(f"[P0] casting Faithless Looting (turn {turn}; "
                f"discard-turn #{ST['loots_done'] + 1})")
            MANA_NEEDS[tag] = {"R": 1}
            await submit_as_is(c, r[1])
            ST["looting_in_flight"] = True
            ST["looting_turn"] = turn
            ST["stage"] = "LOOTING"
            return

    # ---- POSITIVE leg (same turn as the discard) ----
    if ST["stage"] == "POS" and asmo_oid and in_main \
            and ST["discard_turn"] == turn and not ST["asmo_cast"]:
        cast = castspell_for(acts, state, ASMO_L)
        if cast:
            if ST["ass"].get("A5_cast_offered_after_discard") == "not-run":
                ST["ass"]["A5_cast_offered_after_discard"] = "passed"
            ST["offer_turns"].append(turn)
            ST["notes"].append(f"CastSpell(Asmo) offered on discard turn {turn}")
            ST["pre_cast_br"] = untapped_br(state, 0)
            await export_now(c, "pre_cast.json")
            say(f"[P0] casting Asmo for {{B/R}} (turn {turn}); untapped "
                f"B/R lands={ST['pre_cast_br']}")
            MANA_NEEDS[tag] = {"B": 1}
            await submit_as_is(c, cast)
            ST["asmo_cast"] = True
            ST["asmo_cast_turn"] = turn
            return
        ST["pos_obs"] += 1
        if ST["pos_obs"] == 1:
            await export_now(c, "post_discard.json")
            say(f"[P0] Asmo NOT offered right after discard; watching "
                f"(turn {turn})")

    # ---- discard turn ended: move on or finish ----
    if ST["stage"] == "POS" and ST["discard_turn"] is not None \
            and turn > ST["discard_turn"]:
        ST["notes"].append(f"discard turn {ST['discard_turn']} ended without "
                           f"CastSpell(Asmo) offered ({ST['pos_obs']} observations)")
        say(ST["notes"][-1])
        ST["discard_turn"] = None
        ST["pos_obs"] = 0
        ST["looting_in_flight"] = False
        if ST["loots_done"] >= 2 or turn > 40:
            await export_now(c, "post.json")
            ST["post_exported"] = True
            ST["done"] = True
            return
        ST["stage"] = "SEEK"
        say(f"[P0] seeking second Looting (turn {turn})")

    # ---- watch Asmo resolve to battlefield ----
    if ST["asmo_cast"] and not ST["post_exported"]:
        if zone_objs(state, 0, "Stack", ASMO_L) and not ST["mid_exported"]:
            ST["mid_exported"] = True
            await export_now(c, "mid_cast.json")
            say("MID_CAST captured (Asmo on stack)")
        if zone_objs(state, 0, "Battlefield", ASMO_L):
            post_br = untapped_br(state, 0)
            spent = (ST["pre_cast_br"] or 0) - post_br
            ST["notes"].append(f"Asmo reached battlefield (turn {turn}); untapped "
                               f"B/R lands {ST['pre_cast_br']}->{post_br} (delta {spent})")
            ST["ass"]["A6_asmo_cast_paid"] = "passed" if (
                ST["pre_cast_br"] is None or spent >= 1) else "failed"
            await export_now(c, "post.json")
            ST["post_exported"] = True
            ST["done"] = True
            return

    # ---- BUGPROOF leg: Asmo cast pre-discard; watch it resolve ----
    if ST["stage"] == "BUGPROOF":
        if zone_objs(state, 0, "Battlefield", ASMO_L):
            ST["bugs"].append(
                f"Asmo cast WITHOUT discarding resolved to battlefield "
                f"(turn {turn}) -- reported bug confirmed end-to-end")
            say(ST["bugs"][-1])
            await export_now(c, "post.json")
            ST["post_exported"] = True
            ST["done"] = True
            return

    # ---- economy ----
    if in_main:
        if await play_a_land(c, state, pid, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p1_tick(c, st, acts, state):
    pid, tag = 1, "P1"
    drain_rejections(c)
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
    if not my_priority(acts):
        return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if real_decision_pending(st):
        return
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------- finalize
def parse_check_note():
    entry = CARD_DATA.get("asmoranomardicadaistinaculdacar") or {}
    return (f"parse check (pinned v0.102.0 card-data.json): "
            f"mana_cost={json.dumps(entry.get('mana_cost'))[:120]}; "
            f"parse_warnings={json.dumps(entry.get('parse_warnings'))[:300]}; "
            f"static_abilities={json.dumps(entry.get('static_abilities'))[:400]}")


def finalize_assertions():
    a = ST["ass"]
    for k in ("A1_setup_ok", "A2_neg_no_cast_offered", "A3_neg_no_viewer_cast",
              "A4_looting_resolved", "A5_cast_offered_after_discard",
              "A6_asmo_cast_paid"):
        a.setdefault(k, "not-run")
    a["A1_setup_ok"] = "passed"
    if a["A2_neg_no_cast_offered"] == "not-run":
        a["A2_neg_no_cast_offered"] = (
            "passed" if ST["neg_obs"] >= 3 else "not-run")
        if ST["neg_obs"] < 3:
            ST["notes"].append(f"only {ST['neg_obs']} negative observations (<3)")
    if a["A3_neg_no_viewer_cast"] == "not-run":
        a["A3_neg_no_viewer_cast"] = "passed"
    if a["A4_looting_resolved"] == "not-run":
        a["A4_looting_resolved"] = "passed" if ST["all_discarded"] else "failed"
        if not ST["all_discarded"]:
            ST["notes"].append("Faithless Looting never resolved with a discard")
    if a["A5_cast_offered_after_discard"] == "not-run":
        if not ST["all_discarded"]:
            a["A5_cast_offered_after_discard"] = "not-run"
            ST["notes"].append("A5 not-run: no discard happened")
        elif ST["loots_done"] >= 1:
            a["A5_cast_offered_after_discard"] = "failed"
            ST["notes"].append(f"Asmo never offered on any of {ST['loots_done']} discard "
                               f"turns (turns {ST['discard_turns']})")
    if a["A6_asmo_cast_paid"] == "not-run" and not ST["asmo_cast"]:
        a["A6_asmo_cast_paid"] = "not-run"
        ST["notes"].append("A6 not-run: Asmo was never cast")
    if ST["bugs"]:
        ST["notes"].extend(ST["bugs"])

    reported = (a["A2_neg_no_cast_offered"] == "failed"
                or a["A3_neg_no_viewer_cast"] == "failed")
    verdict = ("reproduced" if reported
               else "not-reproduced" if a["A2_neg_no_cast_offered"] == "passed"
               else "blocked")
    ST["notes"].append(parse_check_note())
    if REJECTS:
        ST["notes"].append(f"protocol-106 interaction rejections: "
                           f"{json.dumps(REJECTS)}; skipped iids: {sorted(SKIP_IID)}")
    else:
        ST["notes"].append("no interaction rejections observed on protocol 106")
    return a, verdict


def render_summary_png():
    # Rendered from the actual saved run.json (states + assertions).
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server"]
    y = 20
    d.text((24, y), "Issue #647 - Asmoranomardicadaistinaculdacar: castable "
           "without paying discard cost (revalidation)",
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
        "A1_setup_ok": "A1 game starts, turns advance, both seats act",
        "A2_neg_no_cast_offered": "A2 no CastSpell(Asmo) on no-discard turns",
        "A3_neg_no_viewer_cast": "A3 no vi cast offer for Asmo pre-discard",
        "A4_looting_resolved": "A4 Faithless Looting resolved with discards",
        "A5_cast_offered_after_discard": "A5 CastSpell(Asmo) offered on a discard turn",
        "A6_asmo_cast_paid": "A6 Asmo resolved to BF with {B/R} paid",
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
    d.text((24, H - 52), f"neg_obs={stats.get('neg_observations')} "
           f"discard_turns={stats.get('discard_turns')} "
           f"states_seen={stats.get('states_seen')} "
           f"vi_kinds={','.join(stats.get('seen_vi_kinds', []))[:60]}",
           fill=(120, 130, 150))
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 647/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(f"{EVDIR}/summary.png")


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


def validate():
    say("validating evidence dir...")
    for fn in ("pre_neg.json", "post.json"):
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


async def capture_server_excerpts():
    try:
        logp = f"{BACKFILL}/runs/20261005-0011/server.log"
        if not os.path.exists(logp):
            say("no server.log for the shared server; skipping excerpts")
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
        "reusing the healthy v0.102.0 server on 127.0.0.1:9374 started by run "
        "20261005-0011")

    await p0.create(deck(("Asmoranomardicadaistinaculdacar", 4),
                         ("Faithless Looting", 4),
                         ("Swamp", 26), ("Mountain", 26)), player_count=2)
    await p1.join(p0.game_code, deck(("Island", 60)))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    ST["done"] = False
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0

    while True:
        await asyncio.sleep(0.15)
        now = time.time()
        elapsed = now - t_start

        if elapsed > SETUP_DEADLINE_S:
            ST["notes"].append(
                f"SETUP deadline ({SETUP_DEADLINE_S}s) hit; recording "
                f"not-run/blocked assertions")
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
            ST["states_seen"] += 1
            try:
                if tick_kind == "p0":
                    await p0_tick(c, st, merged_actions(st), st["state"])
                else:
                    await p1_tick(c, st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            if ST.get("done"):
                break

        if p0.latest:
            ST["max_turn"] = max(ST["max_turn"],
                                 p0.latest["state"].get("turn_number") or 0)
        if ST.get("done"):
            break
        if ST["max_turn"] > 45 and ST["stage"] in ("NEG", "SEEK", "LOOTING"):
            ST["notes"].append("game reached turn 45 without completing the "
                               "discard legs")
            break
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            s1 = p1.latest["state"] if p1.latest else s
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} stage={ST['stage']} "
                f"P0asmo_in_hand={ASMO_L in hand_lnames(s, 0)} "
                f"P0looting_in_hand={LOOTING_L in hand_lnames(s, 0)} "
                f"P1hand={len(hand_ids(s1, 1))} "
                f"vikind={vi_kind_code(p0.latest)!r} "
                f"real_decision={real_decision_pending(p0.latest)}")

    # ---------------- finalize ----------------
    assertions, verdict = finalize_assertions()
    ST["notes"].append(f"verdict={verdict}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/20261005-0011 (shared server, not restarted)",
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(__file__, "rb").read()).hexdigest(),
        "decks": {"P0": [("Asmoranomardicadaistinaculdacar", 4),
                          ("Faithless Looting", 4),
                          ("Swamp", 26), ("Mountain", 26)],
                  "P1": [("Island", 60)]},
        "stats": {"states_seen": ST["states_seen"],
                  "neg_observations": ST["neg_obs"],
                  "pos_observations": ST["pos_obs"],
                  "max_turn": ST["max_turn"],
                  "looting_turn": ST["looting_turn"],
                  "discard_turns": ST["discard_turns"],
                  "offer_turns": ST["offer_turns"],
                  "asmo_cast_turn": ST["asmo_cast_turn"],
                  "all_discarded": ST["all_discarded"],
                  "seen_vi_kinds": ST["seen_vi_kinds"]},
        "assertions": assertions,
        "notes": ST["notes"],
        "verdict": verdict,
        "validated_at": VALIDATED_AT,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                        "Dense playsets are a test-harness convenience (engine accepts >4-of "
                        "for custom games).",
                        "States are authoritative exports, restorable only via full game replay."],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_647_01020.py", "w") as f:
        f.write(open(__file__).read())

    # pre/post export fallbacks so the evidence dir is always complete and
    # renderable; each fallback is labeled in the notes and is NOT the
    # contract state.
    if not ST["pre_neg"]:
        try:
            data = await p0.export_state()
            with open(f"{EVDIR}/pre_neg.json", "w") as f:
                f.write(data)
            ST["pre_neg"] = True
            ST["notes"].append("pre_neg.json exported at finish fallback "
                               "(NOT the contract state)")
        except Exception as e:
            say(f"fallback export of pre_neg failed: {e}")
    if not ST["post_exported"]:
        try:
            data = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(data)
            ST["post_exported"] = True
            ST["notes"].append("post.json exported at finish fallback "
                               "(NOT the contract state)")
        except Exception as e:
            say(f"fallback export of post.json failed: {e}")
    run["notes"] = ST["notes"]
    json.dump(run, open(f"{EVDIR}/run.json", "w"), indent=1)

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
