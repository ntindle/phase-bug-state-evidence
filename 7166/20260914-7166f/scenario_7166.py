#!/usr/bin/env python3
"""Issue #7166: Tibalt's Trickery does not work at all, other than
countering a spell.

Oracle: "Counter target spell. Choose 1, 2, or 3 at random. Its controller
mills that many cards, then exiles cards from the top of their library until
they exile a nonland card with a different name than that spell. They may
cast that card without paying its mana cost. Then they put the exiled cards
on the bottom of their library in a random order."

Reported (Discord): only the initial counter occurs; the random mill,
exile-until, free cast, and cleanup sequence never execute. The pinned
card-data parse shows the chain as Counter -> Unimplemented("Choose 1, 2, or
3 at random") -> Mill(count=EventContextAmount) -> ExileFromTopUntil ->
GrantCastingPermission(PlayFromExile) -> PutAtLibraryPosition(Bottom), so the
unimplemented random choice is expected to gate the whole chain.

Plan (native engine, v0.83.0 / protocol 70, two human driver seats):
  P0: 20x Tibalt's Trickery + 40x Mountain (dense playset, cf. #7140).
      Bolt with Trickery in response (targets the Bolt on the stack).
  P1: 20x Mountain + 20x Lightning Bolt + 20x Grizzly Bears. Casts Bolt
      targeting P0 once P0 holds Trickery + 2 untapped Mountains, then
      accepts the free cast of the exiled Bears if the chain offers it.

Behavioral contract:
  A1 setup_ok        P1 cast Lightning Bolt targeting P0; P0 cast Tibalt's
                     Trickery targeting the Bolt on the stack.
  A2 countered       Bolt reached P1's graveyard, P0 life stayed 20 (no 3
                     damage), Trickery resolved to P0's graveyard.
  A3 mill            P1 milled 1-3 cards (gy delta beyond the countered
                     Bolt). failed = 0 (the reported cutoff).
  A4 exile_until     the exile-until stopped at a nonland card with a
                     different name than "Lightning Bolt" from P1's library
                     (Oracle: "their library" = the countered spell's
                     controller). failed = no exile / wrong library.
  A5 free_cast       the exiled Bears was cast for free and reached P1's
                     battlefield (accept branch exercised). not-run if the
                     offer never appeared.
  A6 bottom_rest     P1's exile zone empty at post; P1 card conservation
                     holds (60 cards across zones). not-run if A4 not-run.
  A7 cleanup         no stall; game advanced past the resolution; post
                     exported.

Verdict: reproduced iff A2 passes and (A3 fails or A4 fails) - the counter
         worked but the chain did not. not-reproduced iff A2-A6 pass (or the
         chain executed through the offer and only the accept-branch
         recognizer could not fire, recorded as a limitation).
         blocked iff A1 fails or the resolution window was interrupted.

Driver notes:
  - Target selection for Trickery: candidates are stack spells; the Bolt is
    matched by stack-entry name, falling back to the single candidate.
  - The free cast may surface as a CastSpell legal action for the exiled
    Bears (GrantCastingPermission PlayFromExile) or as a may-cast prompt;
    both are handled, with every opportunity wire-logged.
  - The actually submitted choice id is recorded for target bookkeeping
    (AGENTS.md #6906).
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
RUN_ID = "20260914-7166f"
ISSUE = 7166
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TRICKERY = "Tibalt's Trickery"
BOLT = "Lightning Bolt"
MOUNTAIN = "Mountain"
BEARS = "Grizzly Bears"
P0_DECK = [(TRICKERY, 20), (MOUNTAIN, 40)]
P1_DECK = [(MOUNTAIN, 20), (BOLT, 20), (BEARS, 20)]
TIMEOUT = 1500
STALL_AFTER = 120
TURN_CAP = 30

SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

ST = {}
SUBMITTED = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> CAST -> TRICKERY -> RESOLVING -> DONE
        "stop": False,
        "bolt_cast": False, "bolt_oid": None, "bolt_target_seat": None,
        "bolt_stack_id": None,
        "trickery_cast": False, "trickery_oid": None,
        "trickery_target_choice": None, "trickery_target_stack_id": None,
        "trickery_on_stack": False, "trickery_stack_id": None,
        "trickery_resolved": False, "resolve_turn": None,
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "p0_life_at_cast": None,
        "p1_gy_before": None,
        "mill_count": None,
        "exile_events": [],       # {turn, oid, name, owner, controller}
        "exile_stop_name": None,  # first nonland != Bolt exiled
        "exile_library_owner": None,
        "bears_exiled_oid": None,
        "free_cast_offered": False, "free_cast_offer_shape": None,
        "bears_cast_free": False, "bears_cast_oid": None,
        "bears_on_battlefield": False,
        "p1_discards": 0,
        "post_turn": None,
        "stall_observed": False, "stall_wf": None,
        "last_rev_change": None, "game_started": False, "game_over": False,
        "rejections": [], "turns_seen": set(),
        "prompt_shapes": [],
    })
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})


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


def life_of(state, pid):
    ps = state.get("players") or []
    if pid < len(ps):
        return ps[pid].get("life")
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def stack_spells(state, name=None):
    out = []
    for e in state.get("stack") or []:
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if name is None or nm == name:
            out.append(e)
    return out


def stack_entry_by_source(state, source_oid):
    # Protocol-70 stack entries carry source_id (int), not a card name.
    for e in state.get("stack") or []:
        sid = e.get("source_id")
        if sid is not None and str(sid) == str(source_oid):
            return e
    return None


def bolt_on_stack(state):
    # The Bolt P1 cast, located on the stack without relying on a name
    # field (protocol-70 stack entries carry no card name).
    e = stack_entry_by_source(state, ST.get("bolt_oid"))
    if e is not None:
        return e
    ents = stack_spells(state, "lightning bolt")
    if ents:
        return ents[0]
    # Last resort: P1's turn, Bolt was cast, Trickery not yet cast, and
    # the stack is non-empty while P0 holds priority.
    if ST.get("bolt_cast") and not ST.get("trickery_cast"):
        st = state.get("stack") or []
        if st and state.get("active_player") == 1:
            return st[-1]
    return None


def trickery_on_stack(state):
    e = stack_entry_by_source(state, ST.get("trickery_oid"))
    if e is not None:
        return e
    ents = stack_spells(state, "tibalt's trickery")
    return ents[0] if ents else None


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id") \
                    and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        deep_refs(d, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def vi_opportunities(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def zone_count(state, pid, zone):
    return sum(1 for o in state["objects"].values()
               if o.get("zone") == zone and o.get("owner") == pid)


def p1_gy_names(state):
    return [oname(o) for o in state["objects"].values()
            if o.get("zone") == "Graveyard" and o.get("owner") == 1]


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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} "
        f"choice={cid} ({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)
    return cid


def log_prompt_shape(c, opp, tag, purpose):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    shape = {"who": tag, "purpose": purpose,
             "iid": str(opp.get("interactionId"))[:16],
             "rtype": resp.get("type"),
             "n_choices": len(chs),
             "texts": [choice_text(ch)[:60] for ch in chs[:8]],
             "seats": [seat_of(ch) for ch in chs[:8]],
             "refs": [str(ref_of(ch)) for ch in chs[:8]]}
    ST["prompt_shapes"].append(shape)
    wire("prompt_shape", shape)
    say(f"[{tag}] prompt ({purpose}): rtype={shape['rtype']} "
        f"n={shape['n_choices']} texts={shape['texts']}")


async def answer_target_selection(c, pid, tag, st, state, purpose,
                                 pick_fn):
    """Answer a TargetSelection/CopyRetarget prompt for pid using pick_fn,
    which maps the choice list -> chosen choice (or None to skip)."""
    if wf_type(state) not in ("TargetSelection", "CopyRetarget"):
        return False
    pl = wf_player(state)
    if isinstance(pl, int) and pl != pid:
        return False
    if isinstance(pl, dict) and pl.get("player", pid) != pid:
        return False
    for opp in vi_opportunities(st):
        iid = opp.get("interactionId")
        key = ("tgt", str(iid))
        if key in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        log_prompt_shape(c, opp, tag, purpose)
        pick = pick_fn(chs, state)
        if pick is None:
            say(f"[{tag}] target selection ({purpose}): no matching "
                f"candidate, not answering")
            wire("target_no_match", {"who": tag, "purpose": purpose})
            return False
        cid = await answer_vi(c, opp, pick, tag)
        SUBMITTED.add(key)
        ST["last_target_choice"] = {"who": tag, "purpose": purpose,
                                    "choice_id": cid,
                                    "ref": str(ref_of(pick)),
                                    "seat": seat_of(pick)}
        say(f"[{tag}] answered {purpose}: choice={cid} "
            f"ref={ref_of(pick)} seat={seat_of(pick)}")
        return True
    return False


def pick_bolt_on_stack(chs, state):
    by_id = {str(e.get("id")): e for e in state.get("stack") or []}
    bolt = bolt_on_stack(state)
    bolt_ids = {str(bolt.get("id"))} if bolt is not None else set()
    for ch in chs:
        e = by_id.get(str(ref_of(ch)))
        if e and (str(e.get("name") or e.get("card_name")
                     or "").lower() == "lightning bolt"
                  or str(e.get("id")) in bolt_ids):
            return ch
    if len(chs) == 1:
        return chs[0]
    return None


def pick_seat(seat):
    def _pick(chs, state):
        p = next((ch for ch in chs if seat_of(ch) == seat), None)
        return p if p is not None else (chs[0] if len(chs) == 1 else None)
    return _pick


OPPS_SEEN = set()


async def log_opportunities(c, tag):
    for opp in vi_opportunities(c.latest or {}):
        sig = (opp.get("interactionId"),
               (opp.get("response") or {}).get("type"))
        if sig in OPPS_SEEN:
            continue
        OPPS_SEEN.add(sig)
        log_prompt_shape(c, opp, tag, "ambient")


async def tick_p0(c):
    """P0: mulligan, land drops, counter the Bolt with Trickery, pass."""
    st = c.latest
    if not st:
        return False
    pid = 0
    state, acts = st.get("state"), merged_actions(st)
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) == MOUNTAIN)
            has_t = find_hand(state, pid, TRICKERY) is not None
            keep_ok = (has_t and n_lands >= 2) or MULLS["P0"] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS["P0"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"P0 mulligan -> {choice} (trickery={has_t} lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            h = hand_oids(state, pid)
            names = {o: oname(state["objects"][o]) for o in h}
            # keep Trickery + up to 2 lands on top; bottom the rest
            keep_t = [o for o in h if names[o] == TRICKERY][:1]
            keep_l = [o for o in h if names[o] == MOUNTAIN][:2]
            keep = set(keep_t + keep_l)
            picks = [o for o in h if o not in keep][:count]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"P0 bottoms {len(picks)} after mulligan")
                return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(
            0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        names = {o: oname(state["objects"][o]) for o in h}
        prot = [o for o in h if names[o] == TRICKERY][:1]
        rest = [o for o in h if o not in prot]
        picks = rest[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            say(f"P0 discards {len(picks)}")
            return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"P0 legend-choice submitted as-is: {a['type']}")
            return True
    await log_opportunities(c, "P0")
    # Answer Trickery's target selection: the Bolt on the stack.
    if ST.get("pending_trickery_target"):
        done = await answer_target_selection(
            c, pid, "P0", st, state, "trickery-target", pick_bolt_on_stack)
        if done:
            ST["pending_trickery_target"] = False
            return True
        # fall through to pass if the prompt vanished
        ST["pending_trickery_target"] = False
    # Cast Trickery in response to the Bolt on the stack.
    if not ST["trickery_cast"] and bolt_on_stack(state):
        tid = find_hand(state, pid, TRICKERY)
        if tid and untapped_of(state, pid, MOUNTAIN) >= 2:
            a = castspell_advertised(acts, tid)
            if a:
                ST["p0_life_at_cast"] = life_of(state, 0)
                ST["p1_gy_before"] = zone_count(state, 1, "Graveyard")
                await submit_as_is(c, a)
                ST["trickery_cast"] = True
                ST["trickery_oid"] = int(tid)
                ST["pending_trickery_target"] = True
                ST["stage"] = "TRICKERY"
                say(f"[P0] casts Tibalt's Trickery (oid={tid}) in response "
                    f"to Bolt")
                wire("trickery_cast", {"turn": state.get("turn_number"),
                                       "oid": int(tid)})
                return True
    # Export the pre-resolution state once Trickery is on the stack.
    if ST["trickery_cast"] and not ST["pre_exported"]:
        ent = trickery_on_stack(state)
        if ent is not None:
            ST["trickery_on_stack"] = True
            ST["trickery_stack_id"] = ent.get("id")
            if await export_now("pre_trickery.json") is not None:
                ST["pre_exported"] = True
                # record the actually submitted target (AGENTS.md #6906)
                lt = ST.get("last_target_choice") or {}
                ST["trickery_target_choice"] = lt.get("choice_id")
                ST["trickery_target_stack_id"] = lt.get("ref")
                say(f"pre_trickery exported; target choice="
                    f"{ST['trickery_target_choice']} ref="
                    f"{ST['trickery_target_stack_id']}")
            return True
    if is_my_main(state, pid):
        turn = state.get("turn_number") or 0
        if ST.get("p0_land_turn") != turn:
            lid = find_hand(state, pid, MOUNTAIN)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        ST["p0_land_turn"] = turn
                        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def p0_ready(state):
    """P0 holds Trickery and can pay {1}{R}."""
    return (find_hand(state, 0, TRICKERY) is not None
            and untapped_of(state, 0, MOUNTAIN) >= 2)


async def tick_p1(c):
    """P1: mulligan, land drops, cast Bolt at P0, accept the free cast."""
    st = c.latest
    if not st:
        return False
    pid = 1
    state, acts = st.get("state"), merged_actions(st)
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) == MOUNTAIN)
            has_b = find_hand(state, pid, BOLT) is not None
            keep_ok = (has_b and n_lands >= 1) or MULLS["P1"] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS["P1"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"P1 mulligan -> {choice} (bolt={has_b} lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            h = hand_oids(state, pid)
            names = {o: oname(state["objects"][o]) for o in h}
            keep_b = [o for o in h if names[o] == BOLT][:1]
            keep_l = [o for o in h if names[o] == MOUNTAIN][:1]
            keep = set(keep_b + keep_l)
            picks = [o for o in h if o not in keep][:count]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"P1 bottoms {len(picks)} after mulligan")
                return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(
            0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        names = {o: oname(state["objects"][o]) for o in h}
        pref = [o for o in h if names[o] == MOUNTAIN]
        pref += [o for o in h if o not in pref]
        picks = pref[:n]
        if picks:
            ST["p1_discards"] += len(picks)
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            say(f"P1 discards {len(picks)}")
            return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"P1 legend-choice submitted as-is: {a['type']}")
            return True
    await log_opportunities(c, "P1")
    # Answer Bolt's target selection (target P0) if pending.
    if ST.get("pending_bolt_target"):
        done = await answer_target_selection(
            c, pid, "P1", st, state, "bolt-target", pick_seat(0))
        if done:
            ST["pending_bolt_target"] = False
            return True
        ST["pending_bolt_target"] = False
    # Free cast of the exiled Bears: CastSpell advertised for the exiled
    # card (GrantCastingPermission PlayFromExile), or a may-cast prompt.
    if ST["stage"] in ("RESOLVING", "DONE") or ST["bears_exiled_oid"]:
        for a in acts:
            if a["type"] == "CastSpell":
                oid = str(a.get("data", {}).get("object_id", ""))
                o = state["objects"].get(oid) or {}
                if oname(o) == BEARS and o.get("zone") == "Exile" \
                        and o.get("controller") == 1 \
                        and not ST["bears_cast_free"]:
                    await submit_as_is(c, a)
                    ST["bears_cast_free"] = True
                    ST["bears_cast_oid"] = int(oid)
                    ST["free_cast_offered"] = True
                    ST["free_cast_offer_shape"] = "CastSpell-advertised"
                    say(f"[P1] casts exiled Bears for free (oid={oid})")
                    wire("bears_free_cast",
                         {"turn": state.get("turn_number"), "oid": int(oid)})
                    return True
        # may-cast / optional prompt for player 1: accept iff it names the
        # Bears (or an obvious cast/yes); never blindly answer unknown
        # prompts - record the shape and leave it.
        if wf_player(state) == pid and wf_type(state) in (
                "OptionalEffectChoice", "MayCast", "ChooseCard"):
            for opp in vi_opportunities(st):
                iid = opp.get("interactionId")
                key = ("maycast", str(iid))
                if key in SUBMITTED:
                    continue
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                chs = data.get("choices") or data.get("candidates") or []
                if not chs:
                    continue
                log_prompt_shape(c, opp, "P1", "free-cast-offer")
                ST["free_cast_offered"] = True
                ST["free_cast_offer_shape"] = wf_type(state)
                pick = None
                for ch in chs:
                    t = choice_text(ch).lower()
                    if "cast" in t and "don" not in t and "not" not in t:
                        pick = ch
                        break
                if pick is None:
                    for ch in chs:
                        r = ref_of(ch)
                        o = state["objects"].get(str(r)) or {}
                        if oname(o) == BEARS:
                            pick = ch
                            break
                if pick is not None:
                    await answer_vi(c, opp, pick, "P1")
                    SUBMITTED.add(key)
                    ST["bears_cast_free"] = True
                    say("[P1] accepted the free-cast offer")
                    return True
                say("[P1] free-cast offer shape unrecognized; not answering")
                wire("free_cast_unrecognized",
                     {"wf": wf_type(state), "stage": ST.get("stage")})
                SUBMITTED.add(key)
                return False
    # Cast Bolt at P0 once P0 can answer with Trickery. P0's hand is hidden
    # from P1's viewer-filtered state, so readiness is evaluated from P0's
    # own state in the main loop (ST["p0_can_answer"]).
    if (not ST["bolt_cast"] and is_my_main(state, pid)
            and ST.get("p0_can_answer")):
        bid = find_hand(state, pid, BOLT)
        if bid and untapped_of(state, pid, MOUNTAIN) >= 1:
            a = castspell_advertised(acts, bid)
            if a:
                await submit_as_is(c, a)
                ST["bolt_cast"] = True
                ST["bolt_oid"] = int(bid)
                ST["pending_bolt_target"] = True
                ST["stage"] = "CAST"
                say(f"[P1] casts Lightning Bolt (oid={bid}) targeting P0")
                wire("bolt_cast", {"turn": state.get("turn_number"),
                                   "oid": int(bid)})
                return True
    if is_my_main(state, pid):
        turn = state.get("turn_number") or 0
        if ST.get("p1_land_turn") != turn:
            lid = find_hand(state, pid, MOUNTAIN)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        ST["p1_land_turn"] = turn
                        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def observe(state):
    """Track Trickery resolution: counter, mill, exile-until, free cast."""
    turn = state.get("turn_number") or 0
    objs = state.get("objects") or {}

    # --- exile tracking (the exile-until leg) ---
    for oid, o in objs.items():
        if o.get("zone") != "Exile":
            continue
        if oid in ST.setdefault("exile_seen", set()):
            continue
        ST["exile_seen"].add(oid)
        nm = oname(o)
        ev = {"turn": turn, "oid": int(oid), "name": nm,
              "owner": o.get("owner"), "controller": o.get("controller")}
        ST["exile_events"].append(ev)
        say(f"exiled: {nm} oid={oid} owner={o.get('owner')} turn={turn}")
        wire("exiled", ev)
        # the until-stop: first nonland card with a different name than
        # Lightning Bolt (Oracle: from the countered spell's controller's
        # library)
        is_land = "Land" in str(o.get("card_type") or "") or nm == MOUNTAIN
        if (ST["exile_stop_name"] is None and not is_land
                and nm.lower() != "lightning bolt" and nm):
            ST["exile_stop_name"] = nm
            ST["exile_library_owner"] = o.get("owner")
            say(f"exile-until STOP at {nm} (owner={o.get('owner')})")
            wire("exile_stop", ev)
        if nm == BEARS and o.get("controller") == 1 \
                and ST["bears_exiled_oid"] is None:
            ST["bears_exiled_oid"] = int(oid)
            say(f"Bears exiled for the free cast (oid={oid})")
            # opportunistic mid export: mill + exile-until done, cast pending
            if not ST["mid_exported"] and ST["trickery_resolved"]:
                if await export_now("mid_mill.json") is not None:
                    ST["mid_exported"] = True

    # --- Trickery resolution ---
    if ST["trickery_cast"] and not ST["trickery_resolved"]:
        on_stack = trickery_on_stack(state) is not None
        in_gy = any(oname(o) == TRICKERY and o.get("zone") == "Graveyard"
                    and o.get("controller") == 0 for o in objs.values())
        if in_gy or (ST["trickery_on_stack"] and not on_stack
                     and not (state.get("stack") or [])):
            ST["trickery_resolved"] = True
            ST["resolve_turn"] = turn
            ST["stage"] = "RESOLVING"
            gy = p1_gy_names(state)
            n_bolt = sum(1 for n in gy if n == BOLT)
            ST["mill_count"] = len(gy) - n_bolt - ST["p1_discards"]
            say(f"Trickery resolved turn={turn}; P1 gy={gy} "
                f"mill_count={ST['mill_count']}")
            wire("trickery_resolved",
                 {"turn": turn, "p1_gy": gy,
                  "mill_count": ST["mill_count"],
                  "p0_life": life_of(state, 0)})

    # --- free-cast Bears on the battlefield ---
    if ST["bears_cast_free"] and not ST["bears_on_battlefield"]:
        if any(oname(o) == BEARS and o.get("zone") == "Battlefield"
               and o.get("controller") == 1 for o in objs.values()):
            ST["bears_on_battlefield"] = True
            say(f"free-cast Bears reached P1 battlefield turn={turn}")
            wire("bears_battlefield", {"turn": turn})

    # --- post export: stack empty, no pending decision, quiet window ---
    if ST["stage"] == "RESOLVING" and not ST["post_exported"]:
        quiet = (not (state.get("stack") or [])
                 and wf_type(state) in (None, "Priority"))
        if quiet and not ST.get("post_at"):
            ST["post_at"] = time.time()
        if ST.get("post_at") and time.time() - ST["post_at"] >= 4 and quiet:
            say(f"resolution quiet for 4s; exporting post.json turn={turn}")
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
                ST["post_turn"] = turn
                ST["stage"] = "DONE"
            return
    # after post: play a couple more turns for cleanup, then stop
    if ST["stage"] == "DONE" and ST["post_turn"] is not None:
        if turn >= ST["post_turn"] + 3:
            say(f"cleanup window closed turn={turn}")
            ST["stop"] = True
            return
    if wf_type(state) == "GameOver" and not ST["game_over"]:
        say("game over; exporting post.json before the session expires")
        ST["game_over"] = True
        if not os.path.exists(f"{EVDIR}/post.json"):
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
        ST["stop"] = True


async def main():
    reset()
    ST["exile_seen"] = set()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_created", {"code": p0.game_code,
                          "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                          "p0_deck": P0_DECK, "p1_deck": P1_DECK})
    ST["last_rev_change"] = time.time()

    # binary identity check against the pinned release
    try:
        bh = hashlib.sha256(open(
            f"{BACKFILL}/server/releases/v0.83.0/"
            "phase-server-slim-x86_64-unknown-linux-musl", "rb").read()
        ).hexdigest()
        say(f"server binary sha256={bh[:16]}... "
            f"match={bh == SERVER_IDENTITY['server_binary_sha256']}")
        wire("binary_check", {"match": bh ==
                              SERVER_IDENTITY["server_binary_sha256"]})
    except Exception as e:
        say(f"binary check failed: {e}")

    # parse check: Tibalt's Trickery entry from the pinned card-data
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/"
                             "card-data.json"))
        entry = None
        for _k, v in cd.items():
            if isinstance(v, dict) and v.get("name") == TRICKERY:
                entry = v
                break
        if entry is not None:
            with open(f"{EVDIR}/parse_trickery.json", "w") as f:
                json.dump(entry, f, indent=1)
            chain = []
            ab = (entry.get("abilities") or [None])[0]
            while ab:
                eff = ab.get("effect") or {}
                chain.append(eff.get("type") or eff.get("name"))
                ab = ab.get("sub_ability")
            say(f"parse_trickery.json written; effect chain: {chain}")
            wire("parse_chain", {"chain": chain})
        else:
            say("parse check: Tibalt's Trickery NOT FOUND in card-data")
    except Exception as e:
        say(f"parse check failed: {e}")

    last_rev = {c.name: -1 for c in (p0, p1)}
    last_tick_wall = 0
    clients = [(p0, tick_p0), (p1, tick_p1)]

    async def pump(c, tick):
        now = time.time()
        rej = drain_rejections(c)
        if rej:
            ST["rejections"].extend(
                {"at": now, "who": c.name, "type": r["type"],
                 "data": r["data"]} for r in rej)
        try:
            await tick(c)
        except Exception as e:
            say(f"tick error [{c.name}]: {e}")
        if c.revision != last_rev[c.name]:
            ST["last_rev_change"] = now
            last_rev[c.name] = c.revision

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            for c, tick in clients:
                await pump(c, tick)
        else:
            for c, tick in clients:
                if c.revision != last_rev[c.name]:
                    await pump(c, tick)

        st = p0.latest
        if not st:
            continue
        state = st["state"]
        # P0's counter-readiness from P0's own (unfiltered) view; P1's
        # viewer-filtered state hides P0's hand.
        ST["p0_can_answer"] = p0_ready(state)
        turn = state.get("turn_number") or 0
        if turn >= 1:
            ST["game_started"] = True
            ST["turns_seen"].add(turn)

        if turn >= TURN_CAP and not ST["stop"]:
            say(f"turn cap {TURN_CAP} reached")
            ST["stop"] = True
            continue

        await observe(state)

        if ST["game_started"] and not ST["stop"] \
                and now - ST["last_rev_change"] > STALL_AFTER:
            ST["stall_observed"] = True
            ST["stall_wf"] = {"type": wf_type(state),
                              "player": wf_player(state)}
            say(f"STALL: no revision for {STALL_AFTER}s; "
                f"waiting_for={ST['stall_wf']}")
            wire("stall", ST["stall_wf"])
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

    await finish(p0)
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass

async def finish(p0):
    ass, notes = {}, []

    if not os.path.exists(f"{EVDIR}/post.json"):
        s = await export_now("post.json")
        if s is None:
            say("post.json export failed in finish()")
    states = {}
    for fn in ("pre_trickery", "mid_mill", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as ex3:
            notes.append(f"state reload failed for {fn}.json: {ex3}")
    pre, post = states.get("pre_trickery"), states.get("post")

    def gy_names(s, owner):
        return [oname(o) for o in s["objects"].values()
                if o.get("zone") == "Graveyard" and o.get("owner") == owner]

    def zone_names(s, owner, zone):
        return [oname(o) for o in s["objects"].values()
                if o.get("zone") == zone and o.get("owner") == owner]

    # ---- A1: setup ----
    if ST["bolt_cast"] and ST["trickery_cast"] and ST["pre_exported"]:
        tgt = ST.get("last_target_choice") or {}
        ass["A1_setup_ok"] = "passed"
        notes.append(f"A1: P1 cast Bolt (oid={ST['bolt_oid']}) targeting P0; "
                     f"P0 cast Trickery (oid={ST['trickery_oid']}) with "
                     f"target choice={tgt.get('choice_id')} "
                     f"ref={tgt.get('ref')} (Bolt stack entry)")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1 failed: bolt_cast={ST['bolt_cast']} "
                     f"trickery_cast={ST['trickery_cast']} "
                     f"pre_exported={ST['pre_exported']}")

    # ---- A2: counter ----
    if ass["A1_setup_ok"] == "passed" and post is not None:
        p1gy = gy_names(post, 1)
        bolt_gy = BOLT in p1gy
        trickery_gy = TRICKERY in gy_names(post, 0)
        p0life = (post.get("players") or [{}])[0].get("life")
        if bolt_gy and trickery_gy and p0life == 20:
            ass["A2_countered"] = "passed"
            notes.append(f"A2: Bolt countered to P1 gy; Trickery resolved "
                         f"to P0 gy; P0 life={p0life} (no 3 damage)")
        else:
            ass["A2_countered"] = "failed"
            notes.append(f"A2 FAILED: bolt_gy={bolt_gy} "
                         f"trickery_gy={trickery_gy} p0_life={p0life}")
    elif ass["A1_setup_ok"] == "passed":
        ass["A2_countered"] = "failed"
        notes.append("A2 failed: post.json missing")
    else:
        ass["A2_countered"] = "not-run"
        notes.append("A2 not-run: setup failed")

    # ---- A3: mill ----
    if ass["A2_countered"] == "passed":
        mc = ST["mill_count"]
        if mc is not None and 1 <= mc <= 3:
            ass["A3_mill"] = "passed"
            notes.append(f"A3: P1 milled {mc} cards (random 1-3 choice "
                         f"executed)")
        elif mc == 0:
            ass["A3_mill"] = "failed"
            notes.append("A3 FAILED: P1 milled 0 cards - the chain stopped "
                         "after the counter (reported bug)")
        else:
            ass["A3_mill"] = "failed"
            notes.append(f"A3 FAILED: mill_count={mc} (expected 1-3)")
    else:
        ass["A3_mill"] = "not-run"
        notes.append("A3 not-run: counter leg not established")

    # ---- A4: exile-until ----
    if ass["A2_countered"] == "passed":
        stop = ST["exile_stop_name"]
        owner = ST["exile_library_owner"]
        if stop and stop.lower() != "lightning bolt" and owner == 1:
            ass["A4_exile_until"] = "passed"
            notes.append(f"A4: exile-until stopped at {stop} from P1's "
                         f"library (nonland, different name than Bolt)")
        elif ST["exile_events"]:
            ass["A4_exile_until"] = "failed"
            notes.append(f"A4 FAILED: exile events={ST['exile_events']}; "
                         f"stop={stop} owner={owner} (expected a non-Bolt "
                         f"nonland from P1's library)")
        else:
            ass["A4_exile_until"] = "failed"
            notes.append("A4 FAILED: no cards exiled - the chain stopped "
                         "after the counter (reported bug)")
    else:
        ass["A4_exile_until"] = "not-run"
        notes.append("A4 not-run: counter leg not established")

    # ---- A5: free cast ----
    if ass["A4_exile_until"] == "passed":
        if ST["bears_on_battlefield"]:
            ass["A5_free_cast"] = "passed"
            notes.append(f"A5: exiled Bears cast for free "
                         f"(offer={ST['free_cast_offer_shape']}) and "
                         f"reached P1's battlefield")
        elif ST["free_cast_offered"]:
            ass["A5_free_cast"] = "not-run"
            notes.append(f"A5 not-run: free-cast offer appeared "
                         f"({ST['free_cast_offer_shape']}) but the accept "
                         f"branch could not be driven to the battlefield")
        else:
            ass["A5_free_cast"] = "failed"
            notes.append("A5 FAILED: Bears exiled but no free-cast offer "
                         "ever appeared")
    else:
        ass["A5_free_cast"] = "not-run"
        notes.append("A5 not-run: exile-until leg not established")

    # ---- A6: bottom rest + conservation ----
    if ass["A4_exile_until"] == "passed" and post is not None:
        p1exile = zone_names(post, 1, "Exile")
        p0exile = zone_names(post, 0, "Exile")
        total_p1 = sum(1 for o in post["objects"].values()
                       if o.get("owner") == 1)
        if not p1exile and not p0exile and total_p1 == 60:
            ass["A6_bottom_rest"] = "passed"
            notes.append(f"A6: exile zones empty; P1 card conservation "
                         f"holds (60/60) - exiled cards bottomed")
        else:
            ass["A6_bottom_rest"] = "failed"
            notes.append(f"A6 FAILED: p1_exile={p1exile} p0_exile={p0exile} "
                         f"p1_total={total_p1}/60")
    else:
        ass["A6_bottom_rest"] = "not-run"
        notes.append("A6 not-run: exile-until leg not established")

    # ---- A7: cleanup ----
    post_ok = post is not None
    if ST["game_over"] and not post_ok:
        ok = not ST["stall_observed"]
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append("A7: game over (post unexportable); "
                     f"stall={ST['stall_observed']}")
    elif post_ok:
        ok = (not ST["stall_observed"]
              and len(ST["turns_seen"]) >= 3
              and (len(post.get("stack") or []) == 0 or ST["game_over"]))
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A7: stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])} "
                     f"stack_empty={len(post.get('stack') or []) == 0}")
    else:
        ass["A7_cleanup"] = "failed"
        notes.append("A7 failed: post.json missing")

    for k, v in ass.items():
        say(f"{k}: {v}")
    for n in notes:
        say("note:", n)

    if ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif (ass["A2_countered"] == "passed"
          and ass["A3_mill"] == "failed"):
        verdict = "reproduced"
    elif (ass["A2_countered"] == "passed"
          and ass["A4_exile_until"] == "failed"):
        verdict = "reproduced"
    elif all(ass.get(k) == "passed" for k in (
            "A1_setup_ok", "A2_countered", "A3_mill", "A4_exile_until",
            "A5_free_cast", "A6_bottom_rest")):
        verdict = "not-reproduced"
    elif (ass["A2_countered"] == "passed"
          and ass["A3_mill"] == "passed"
          and ass["A4_exile_until"] == "passed"
          and ass["A5_free_cast"] == "not-run"):
        # chain executed through the offer; only the accept-branch
        # recognizer could not fire - the reported "nothing after counter"
        # is disproven by the mill + exile.
        verdict = "not-reproduced"
        notes.append("limitation: free-cast offer appeared but the accept "
                     "branch was not driven (unrecognized offer shape)")
    elif ST["trickery_cast"] and not ST["trickery_resolved"]:
        verdict = "blocked"
        notes.append("blocked: Trickery cast but resolution never observed")
    else:
        verdict = "blocked"
    say("VERDICT:", verdict)

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Tibalt's Trickery does not work at all, other than "
                 "countering a spell",
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "scope": "Native engine, two human-client seats; P1 casts Lightning "
                 "Bolt at P0, P0 counters with Tibalt's Trickery targeting "
                 "the Bolt; the post-counter chain (random mill, "
                 "exile-until, free cast, bottom rest) is exercised to "
                 "explicit assertions",
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "driver_state": {
            "bolt_oid": ST["bolt_oid"],
            "trickery_oid": ST["trickery_oid"],
            "trickery_target_choice": ST["trickery_target_choice"],
            "trickery_target_stack_id": ST["trickery_target_stack_id"],
            "resolve_turn": ST["resolve_turn"],
            "mill_count": ST["mill_count"],
            "exile_events": ST["exile_events"],
            "exile_stop_name": ST["exile_stop_name"],
            "exile_library_owner": ST["exile_library_owner"],
            "bears_exiled_oid": ST["bears_exiled_oid"],
            "free_cast_offered": ST["free_cast_offered"],
            "free_cast_offer_shape": ST["free_cast_offer_shape"],
            "bears_cast_free": ST["bears_cast_free"],
            "bears_on_battlefield": ST["bears_on_battlefield"],
            "p1_discards": ST["p1_discards"],
            "turns_seen": sorted(ST["turns_seen"]),
            "rejections": ST["rejections"][:10],
            "prompt_shapes": ST["prompt_shapes"][:12],
        },
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "The 'random order' of bottomed cards is not verifiable from "
            "state; A6 asserts exile-empty + card conservation only.",
            "No combat occurs; both seats decline all attacks/blocks.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")
    shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7166.py")
    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
    except Exception as ex4:
        notes.append(f"server.log copy failed: {ex4}")
        say("server.log copy failed:", ex4)
    render_png(run)
    # Close the logs BEFORE hashing: the manifest must cover the final
    # bytes of scenario_run.log / wire_log.jsonl (cf. AGENTS.md #6916).
    WIRE.close()
    RUNLOG.close()

    def emit(*a):
        print(" ".join(str(x) for x in a), flush=True)

    files = ["pre_trickery.json", "mid_mill.json", "post.json",
             "parse_trickery.json", "run.json", "scenario_7166.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            emit(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    emit("wrote manifest.sha256")
    for fn in ("pre_trickery.json", "mid_mill.json", "post.json",
               "parse_trickery.json", "run.json"):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            json.load(open(p))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
    for line in man:
        h, fn = line.split("  ")
        assert hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
    emit("validation: all JSON parse, PNG readable, hashes match")


def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#7166 - Tibalt's Trickery does not work at all, other "
           "than countering a spell", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-14 - "
           "native engine, 2 human seats", fill=(140, 160, 180))
    y += 28
    col = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: Counter target spell. Choose 1, 2, or 3 at "
           "random. Its controller mills", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "that many cards, then exiles until a nonland card with "
           "a different name; they may", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "cast it for free; the rest go to the bottom in random "
           "order.", fill=(200, 210, 225))
    y += 34
    labels = {
        "A1_setup_ok": "GAME: P1 Bolt at P0; P0 Trickery targets the Bolt "
                     "on the stack (pre_trickery.json)",
        "A2_countered": "GAME: Bolt countered to P1 gy; P0 at 20 life",
        "A3_mill": "GAME: P1 mills 1-3 cards (REPORTED BUG: 0 - chain "
                 "stops after counter)",
        "A4_exile_until": "GAME: exile-until stops at a non-Bolt nonland "
                        "from P1's library",
        "A5_free_cast": "GAME: exiled Bears cast for free, reaches "
                      "battlefield",
        "A6_bottom_rest": "GAME: exile empty at post; P1 60/60 conserved",
        "A7_cleanup": "GAME: no stall; game advanced past resolution",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v}", fill=c)
        y += 22
        d.text((52, y), lab[:104], fill=(150, 160, 175))
        y += 26
    y += 8
    ds = run.get("driver_state") or {}
    d.text((24, y), f"resolve_turn={ds.get('resolve_turn')} "
           f"mill={ds.get('mill_count')} "
           f"exile_stop={ds.get('exile_stop_name')} "
           f"exile_owner={ds.get('exile_library_owner')} "
           f"free_cast={ds.get('bears_cast_free')}",
           fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


if __name__ == "__main__":
    asyncio.run(main())
