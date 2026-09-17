#!/usr/bin/env python3
"""Issue #6301 revalidation on v0.85.0 (protocol 72): Force of Will alt-cost.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github, confirmed, priority:p0-softlock; labels area:engine,
area:frontend, mechanic:costs): "As a response to a Fatal Push I try to cast
a Force of Will. UI hangs with `Casting...`". Expected: cast Force of Will by
paying 1 life and exiling a blue card from hand.

Oracle text (pinned card-data.json, key "force of will"):
  You may pay 1 life and exile a blue card from your hand rather than pay
  this spell's mana cost.
  Counter target spell.

Reporter's exact position (attached game state, inspected 2026-09-16):
  Turn 4, P0 priority. Stack: P0's Brainstorm (beneath), P1's Fatal Push
  (top, targeting P0's creature). P0 hand: Dark Ritual, Murktide Regent,
  Force of Will, Thoughtseize. P0: one untapped Underground Sea (3UU
  unaffordable -> alt cost is the only way). Exactly one blue pitch card
  (Murktide Regent; FoW cannot exile itself).

This run reconstructs that SHAPE on the pinned release (the 2026-09-10 run
used ONE spell on the stack, so FoW's target was auto-chosen; rykerwilliams'
2026-09-15 comment notes the reporter's position needs a REAL target prompt
with two spells on the stack):
  P0: Underground Seas (exactly 2, so 3UU is unpayable and the alt cost is
      the only path), Memnite on the battlefield (stands in for the
      reporter's crewed The Fantasticar as Fatal Push's victim - documented
      deviation: crewing a Vehicle adds failure surface without changing the
      FoW cost-flow under test), hand: Brainstorm + Force of Will +
      Murktide Regent.
  P1: Underground Seas, Fatal Push in hand.
  Flow: P0 casts Brainstorm -> P1 responds with Fatal Push targeting Memnite
  -> P0 responds with Force of Will (alt cost), must pick a target between
  the two stack spells, exile Murktide Regent, pay 1 life.

Expected:
  E1: Brainstorm on stack; P1 priority; P1 casts Fatal Push -> 2 spells on
      stack; P0 priority.
  E2: CastSpell(Force of Will) advertised to P0 (alt cost is the only way).
  E3: OptionalCostChoice offers pay=true; submitting it pays 1 life.
  E4: exile-a-blue-card schema prompt (intent=exile) offers Murktide Regent;
      answering exiles exactly Murktide.
  E5: TargetSelection offers BOTH stack spells as candidates (the key
      difference vs the 2026-09-10 run); choosing Fatal Push works.
  E6: FoW resolves: Fatal Push countered (-> P1 GY), Brainstorm resolves
      (draw 3, put 2 back -> P0 GY), Memnite survives, P0 at 19 life.
  E7: no stuck decision; game proceeds.

Assertions:
  A1_setup_ok       pre.json: Brainstorm + Fatal Push on stack (Brainstorm
                    beneath), P0 priority, FoW + Murktide in P0 hand,
                    Memnite on P0 BF, life 20/20
  A2_cast_offered   CastSpell(Force of Will) advertised to P0 while both
                    spells are on the stack
  A3_alt_cost_paid  OptionalCostChoice pay=true submitted; P0 life 20 -> 19
  A4_exile_answered exile prompt presented Murktide Regent as a candidate
                    and was answered with it; Murktide in Exile
  A5_target_prompted TargetSelection offered both stack spells as candidates
  A6_fow_resolves   post.json: Fatal Push in P1 GY (countered), Brainstorm
                    in P0 GY (resolved), Memnite on P0 BF, life 19/20
  A7_no_stall       stack empty, no pending FoW decision, game proceeds

Verdict: reproduced iff the FoW alternative-cost cast fails to complete
engine-side (the reported "can't be cast" stall); not-reproduced iff
A1..A7 all pass (points at the frontend half); blocked iff the setup
cannot be assembled or driven.
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

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260916-6301"
EVDIR = f"{BACKFILL}/evidence/6301/{RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "server_binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
    "signature_verified": True,
}

# recompute against on-disk artifacts; never copy hashes blindly (#7176)
for _f, _k in (("server/releases/v0.85.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.85.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.85.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

FOW = "Force of Will"
BRAINSTORM = "Brainstorm"
FATAL_PUSH = "Fatal Push"
MURKTIDE = "Murktide Regent"
MEMNITE = "Memnite"
SEA = "Underground Sea"

P0_DECK = [(FOW, 12), (BRAINSTORM, 10), (MURKTIDE, 10), (MEMNITE, 8), (SEA, 20)]
P1_DECK = [(FATAL_PUSH, 12), (SEA, 48)]

# ---- shared mutable run state ----
ST = {
    "mulls": {},
    "submitted_iid": set(),
    "obs": {"cast_offered": False, "target_candidates": [],
            "exile_candidates": [], "rejections": []},
    "brainstorm_cast": False,
    "fatal_push_cast": False,
    "fow_cast_attempted": False,
    "pre_exported": False,
    "mid_exported": False,
    "post_exported": False,
    "brainstorm_putback_done": False,
    "done": False,
}


def obj_name(state, oid):
    o = (state.get("objects") or {}).get(str(oid))
    return str(o.get("base_name") or o.get("name") or "?") if o else "?"


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_oids(state, pid):
    return [str(x) for x in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [obj_name(state, x) for x in hand_oids(state, pid)]


def bf_objs(state, pid):
    return [o for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_bf(state, pid, name):
    for o in bf_objs(state, pid):
        if obj_name(state, o.get("id")) == name:
            return o.get("id")
    return None


def count_lands_bf(state, pid):
    return sum(1 for o in bf_objs(state, pid) if obj_name(state, o.get("id")) == SEA)


def stack_entries(state):
    return state.get("stack") or []


def stack_card_names(state):
    """Resolve each stack entry's source card name (never guess)."""
    out = []
    for e in stack_entries(state):
        ref = e.get("source_id") or e.get("source") or e.get("object_id")
        nm = obj_name(state, ref) if ref is not None else "?"
        out.append((e.get("id"), nm))
    return out


def spell_on_stack(state, name):
    return any(nm == name for _sid, nm in stack_card_names(state))


def fow_zone(state):
    """Where the cast Force of Will object currently is (Stack/Graveyard/...)."""
    for o in (state.get("objects") or {}).values():
        if obj_name(state, o.get("id")) == FOW and o.get("zone") in ("Stack", "Graveyard", "Exile"):
            # the cast copy: prefer the one most recently on the stack
            if o.get("zone") == "Stack":
                return "Stack"
    for o in (state.get("objects") or {}).values():
        if obj_name(state, o.get("id")) == FOW and o.get("zone") == "Graveyard":
            return "Graveyard"
    return None


def in_gy(state, pid, name):
    return any(obj_name(state, o) == name
               for o in player_of(state, pid).get("graveyard", []))


def exiled_named(state, name):
    return [str(o.get("id")) for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Exile" and obj_name(state, o.get("id")) == name]


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def pending_for(state, pid):
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def is_blue(state, oid):
    o = (state.get("objects") or {}).get(str(oid)) or {}
    cols = o.get("color") or o.get("base_color") or []
    return "Blue" in cols


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def do_mulligan(c, pid, tag, keep_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    acts = merged_actions(st)
    a = next((x for x in acts if x.get("type") == "MulliganDecision"), None)
    if not a or tag in ST["mulls"]:
        return False
    hn = hand_names(state, pid)
    n = ST["mulls"].get((tag, "n"), 0)
    if keep_fn(hn) or n >= 3:
        sub = copy.deepcopy(a)
        sub["data"]["decision"] = "keep"
        say(f"[{tag}] mulligan keep (hand={[h for h in hn][:8]} mulls={n})")
        await submit_as_is(c, sub)
        ST["mulls"][tag] = True
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn[:10]})
    else:
        sub = copy.deepcopy(a)
        sub["data"]["decision"] = "mulligan"
        ST["mulls"][(tag, "n")] = n + 1
        say(f"[{tag}] mulligan #{n + 1}")
        await submit_as_is(c, sub)
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    return True


async def do_bottom(c, pid, tag, protect):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    n = int((pend.get("phase") or {}).get("count") or 1)
    acts = merged_actions(st)
    a = next((x for x in acts if x.get("type") == "SelectCards"), None)
    if not a or (tag, "bottomed") in ST["mulls"]:
        return False

    def rank(oid):
        nm = obj_name(state, oid)
        if nm == SEA:
            return 0
        if nm in protect:
            return 3
        return 1

    picks = sorted(hand_oids(state, pid), key=rank)[:n]
    sub = copy.deepcopy(a)
    sub["data"]["cardIds"] = [int(x) for x in picks]
    say(f"[{tag}] bottoming {n}: {[obj_name(state, x) for x in picks]}")
    await submit_as_is(c, sub)
    ST["mulls"][(tag, "bottomed")] = True
    return True


def discard_rank(state, oid, protect):
    nm = obj_name(state, oid)
    if nm == SEA:
        return 0
    if nm in protect:
        return 3
    return 1


async def do_discard(c, pid, tag, protect):
    """Protocol-72 DiscardToHandSize: advertised SelectCards legal action,
    one per discardable card (data.cards=[oid]). Gated on the named player."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    acts = [a for a in merged_actions(st)
            if a.get("type") == "SelectCards" and (a.get("data") or {}).get("cards")]
    if not acts:
        return False
    pick = sorted(acts, key=lambda a: discard_rank(
        state, (a.get("data") or {}).get("cards", [None])[0], protect))[0]
    oid = pick["data"]["cards"][0]
    say(f"[{tag}] discarding to hand size: {obj_name(state, oid)} (oid {oid})")
    wire("discard_action", {"who": tag, "action": pick})
    await submit_as_is(c, pick)
    return True


async def pay_tick(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and str((wf.get("data") or {}).get("player")) == str(pid)


def my_main(state, pid):
    return (state.get("active_player") == pid and my_priority(state, pid)
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


_PASSED_REV = {}
_WATCH = {}


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(wf_of(s).get('type'))} pp={s.get('priority_player')} "
                    f"stack={[n for _, n in stack_card_names(s)]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


def cand_object_name(state, ch):
    """Resolve a candidate's referenced object name (never guess)."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        if d.get("reference") is not None:
            return obj_name(state, d["reference"])
        if d.get("name"):
            return str(d["name"])
    return ""


def cand_zone(state, ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        if d.get("zone"):
            return str(d["zone"])
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        if d.get("reference") is not None:
            o = (state.get("objects") or {}).get(str(d["reference"]))
            if o:
                return o.get("zone")
    return None


def selection_intent(opp):
    for s in opp.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        if d.get("intent"):
            return str(d["intent"])
    return None


async def scan_interactions(st, who, c):
    """Answer P0's FoW flow prompts + P1's Fatal Push target prompt.
    Priority menus (exactChoices with action-code surfaces) are never touched.
    Returns True if it submitted something."""
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return False
    state = st["state"]
    pid = 0 if who == "P0" else 1
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        iid = opp.get("interactionId")
        if not iid or iid in ST["submitted_iid"]:
            continue
        if rtype == "exactChoices":
            # OptionalCostChoice: decideOptionalCost + pay=true value surface
            for ch in data.get("choices", []):
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", [])]
                if "decideOptionalCost" not in codes:
                    continue
                if who != "P0" or not ST["fow_cast_attempted"]:
                    continue
                is_pay = any(s.get("data", {}).get("role") == "pay"
                             and str(s.get("data", {}).get("value")).lower() == "true"
                             for s in ch.get("surfaces", []))
                if not is_pay:
                    continue
                say(f"[P0] OptionalCostChoice -> PAY (alt cost: 1 life + exile blue)")
                sub = {"interactionId": iid,
                       "response": {"type": "choose", "data": {"choiceId": ch["id"]}}}
                wire("optcost_pay", {"submission": sub})
                await c.send_interaction(sub)
                ST["submitted_iid"].add(iid)
                ST["obs"]["alt_cost_seen"] = True
                return True
            continue  # never touch other exactChoices (priority menus)
        if rtype != "schema":
            continue
        spec = data.get("spec", {}) or {}
        spec_type = spec.get("type")
        cands = data.get("candidates", []) or []
        intent = selection_intent(opp)

        def submit_ids(ids):
            return {"interactionId": iid,
                    "response": {"type": spec_type, "data": {"choiceIds": ids}}}

        # --- FoW's exile-a-blue-card: schema select, intent=exile ---
        if (intent == "exile" and who == "P0" and ST["fow_cast_attempted"]
                and fow_zone(state) != "Stack"):
            ST["obs"]["exile_candidates"] = sorted({cand_object_name(state, ch) for ch in cands})
            pick = next((ch for ch in cands
                         if cand_object_name(state, ch) == MURKTIDE), None)
            if pick is None:
                # fallback: any blue card in hand (the cast copy is on no
                # zone we can pitch from; other FoW copies are legal)
                def ref_oid(ch):
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        if d.get("reference") is not None:
                            return str(d["reference"])
                    return None
                pick = next((ch for ch in cands
                             if ref_oid(ch) and is_blue(state, ref_oid(ch))), None)
            if pick is None:
                say(f"[P0] exile prompt with no blue candidate; candidates={ST['obs']['exile_candidates']}")
                wire("exile_no_blue", {"interaction": opp})
                continue
            say(f"[P0] exile choice -> {cand_object_name(state, pick)}")
            sub = submit_ids([pick["id"]])
            wire("exile_choice", {"submission": sub,
                                  "available": ST["obs"]["exile_candidates"]})
            await c.send_interaction(sub)
            ST["submitted_iid"].add(iid)
            ST["obs"]["exile_answered"] = cand_object_name(state, pick)
            return True

        # --- target selection: candidates on the stack ---
        zones = {cand_zone(state, ch) for ch in cands}
        # NOTE: candidate zone strings arrive lowercase ("stack") while the
        # authoritative state uses "Stack" - compare case-insensitively.
        if zones == {"Stack"} or any(str(cand_zone(state, ch) or "").lower() == "stack" for ch in cands):
            names = sorted({cand_object_name(state, ch) for ch in cands})
            if who == "P1" and ST["fatal_push_cast"] and not ST["fow_cast_attempted"]:
                # Fatal Push's target (single legal target normally auto-
                # targeted; handled defensively): Memnite
                pick = next((ch for ch in cands
                             if cand_object_name(state, ch) == MEMNITE), None)
                if pick is None:
                    continue
                say("[P1] Fatal Push target -> Memnite")
                wire("fatal_push_target", {"submission": submit_ids([pick["id"]])})
                await c.send_interaction(submit_ids([pick["id"]]))
                ST["submitted_iid"].add(iid)
                return True
            if who == "P0" and ST["fow_cast_attempted"]:
                # FoW's target: MUST offer both stack spells (A5)
                ST["obs"]["target_candidates"] = names
                say(f"[P0] FoW target prompt candidates: {names}")
                # export MID_TARGET before submitting (A5 evidence)
                if not ST["mid_exported"]:
                    try:
                        mid = await p0.export_state()
                        with open(f"{EVDIR}/mid_target.json", "w") as f:
                            f.write(mid)
                        ST["mid_exported"] = True
                        say("mid_target.json exported (TargetSelection pending)")
                    except Exception as e:
                        say(f"mid export failed: {e}")
                pick = next((ch for ch in cands
                             if cand_object_name(state, ch) == FATAL_PUSH), None)
                if pick is None:
                    say(f"[P0] FoW target prompt without Fatal Push candidate")
                    wire("fow_target_no_push", {"interaction": opp})
                    continue
                say("[P0] FoW target -> Fatal Push")
                sub = submit_ids([pick["id"]])  # one slot per prompt (#7179)
                wire("fow_target", {"submission": sub, "candidates": names})
                await c.send_interaction(sub)
                ST["submitted_iid"].add(iid)
                ST["obs"]["fow_target_choice"] = FATAL_PUSH
                return True
            continue

        # --- Brainstorm put-back: schema card choice, hand candidates,
        #     NOT the exile prompt, after FoW resolved ---
        if (who == "P0" and cands
                and all(str(cand_zone(state, ch) or "").lower() == "hand" for ch in cands)
                and fow_zone(state) == "Graveyard"
                and not ST["brainstorm_putback_done"]):
            constraint = (spec.get("data", {}) or {}).get("constraint", {}) or {}
            cdata = constraint.get("data", {}) or {}
            want = int(cdata.get("max") or 2)
            def putback_rank(ch):
                nm = cand_object_name(state, ch)
                if nm == SEA:
                    return 0
                if nm in (FOW, BRAINSTORM, MURKTIDE, MEMNITE):
                    return 3
                return 1
            picks = sorted(cands, key=putback_rank)[:want]
            say(f"[P0] Brainstorm put-back -> {[cand_object_name(state, ch) for ch in picks]}")
            sub = submit_ids([ch["id"] for ch in picks])
            wire("brainstorm_putback", {"submission": sub})
            await c.send_interaction(sub)
            ST["submitted_iid"].add(iid)
            ST["brainstorm_putback_done"] = True
            return True
    return False


async def p0_tick(st, acts):
    c = p0
    state = st["state"]
    if await do_mulligan(c, 0, "P0", lambda hn: FOW in hn and BRAINSTORM in hn and MURKTIDE in hn):
        return
    if await do_bottom(c, 0, "P0", {FOW, BRAINSTORM, MURKTIDE, MEMNITE}):
        return
    if await do_discard(c, 0, "P0", {FOW, BRAINSTORM, MURKTIDE, MEMNITE}):
        return
    if await pay_tick(c):
        return
    # declare no attackers while assembling (keep the game moving through
    # combat so draw steps keep coming)
    if (wf_of(state).get("type") == "DeclareAttackers"
            and state.get("active_player") == 0):
        da = next((x for x in acts if x.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return
    # SelectCards-as-exile fallback (protocol-72 may surface the exile as a
    # legal action instead of an interaction): only while FoW is in flight.
    if (wf_of(state).get("type") == "SelectCards" and ST["fow_cast_attempted"]
            and fow_zone(state) != "Stack" and "exile_answered" not in ST["obs"]):
        sacts = [a for a in acts if a.get("type") == "SelectCards"
                 and (a.get("data") or {}).get("cards")]
        if sacts:
            def rank_exile(a):
                oid = str((a.get("data") or {}).get("cards", [None])[0])
                nm = obj_name(state, oid)
                if nm == MURKTIDE:
                    return 0
                return 1 if is_blue(state, oid) else 2
            pick = sorted(sacts, key=rank_exile)[0]
            oid = pick["data"]["cards"][0]
            say(f"[P0] SelectCards exile fallback -> {obj_name(state, oid)}")
            wire("exile_selectcards", {"action": pick})
            await submit_as_is(c, pick)
            ST["obs"]["exile_answered"] = obj_name(state, oid)
            return
    # Brainstorm put-back as a legal action fallback
    if (wf_of(state).get("type") == "SelectCards" and fow_zone(state) == "Graveyard"
            and not ST["brainstorm_putback_done"] and not ST["fow_cast_attempted"] is False):
        pass  # handled via interaction path; SelectCards here is ambiguous
    if await scan_interactions(st, "P0", c):
        return
    # PRE export: both spells on stack, P0 priority, before the FoW cast
    if (not ST["pre_exported"] and not ST["fow_cast_attempted"]
            and spell_on_stack(state, BRAINSTORM) and spell_on_stack(state, FATAL_PUSH)
            and my_priority(state, 0)):
        say("PRE: Brainstorm + Fatal Push on stack, P0 priority; exporting pre.json")
        try:
            pre = await p0.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            say("pre.json exported")
        except Exception as e:
            say(f"pre export failed: {e}")
        ST["pre_exported"] = True
        return  # next tick casts with fresh actions
    if not my_priority(state, 0):
        return
    # --- P0 priority decisions ---
    # 1. Respond to Fatal Push with FoW. This MUST precede the main-phase
    #    setup branch: with 2 spells on the stack during P0's own
    #    PreCombatMain, my_main() is true, and the old if/else fell through
    #    to a plain PassPriority, running the stack away (first attempt).
    if (spell_on_stack(state, FATAL_PUSH) and not ST["fow_cast_attempted"]
            and FOW in hand_names(state, 0)):
        a = next((x for x in acts if x.get("type") == "CastSpell"
                  and obj_name(state, (x.get("data") or {}).get("object_id")) == FOW), None)
        if a:
            ST["obs"]["cast_offered"] = True
            say("P0 casts Force of Will in response to Fatal Push")
            wire("cast_fow", a)
            await submit_as_is(c, a)
            ST["fow_cast_attempted"] = True
            return
        # CastSpell not advertised yet: hold priority (the reported bug
        # is exactly "can't be cast" - never pass into a resolve).
        if not ST["obs"].get("no_fow_logged"):
            ST["obs"]["no_fow_logged"] = True
            ST["no_fow_since"] = time.time()
            say(f"[P0] 2 spells on stack but no CastSpell(FoW) advertised; "
                f"actions={sorted({x['type'] for x in acts})} (holding priority)")
            wire("no_fow_castspell",
                 {"actions": sorted({x["type"] for x in acts}),
                  "hand": hand_names(state, 0)[:8]})
        return
    # 2. main-phase setup
    if my_main(state, 0):
        hn = hand_names(state, 0)
        # setup: exactly 2 lands (3UU must stay unpayable -> alt-cost only)
        if count_lands_bf(state, 0) < 2:
            for a in acts:
                if a.get("type") == "PlayLand":
                    await submit_as_is(c, a)
                    say("P0 plays Underground Sea")
                    return
        if find_bf(state, 0, MEMNITE) is None and MEMNITE in hn:
            a = next((x for x in acts if x.get("type") == "CastSpell"
                      and obj_name(state, (x.get("data") or {}).get("object_id")) == MEMNITE), None)
            if a:
                say("P0 casts Memnite")
                wire("cast_memnite", a)
                await submit_as_is(c, a)
                return
        # trigger: all pieces ready, stack empty
        if (not ST["brainstorm_cast"] and not stack_entries(state)
                and BRAINSTORM in hn and FOW in hn and MURKTIDE in hn
                and find_bf(state, 0, MEMNITE) is not None):
            a = next((x for x in acts if x.get("type") == "CastSpell"
                      and obj_name(state, (x.get("data") or {}).get("object_id")) == BRAINSTORM), None)
            if a:
                say("P0 casts Brainstorm (trigger)")
                wire("cast_brainstorm", a)
                await submit_as_is(c, a)
                ST["brainstorm_cast"] = True
                return
    # default: pass priority when holding it
    for a in acts:
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            return


async def p1_tick(st, acts):
    c = p1
    state = st["state"]
    if await do_mulligan(c, 1, "P1", lambda hn: FATAL_PUSH in hn):
        return
    if await do_bottom(c, 1, "P1", {FATAL_PUSH}):
        return
    if await do_discard(c, 1, "P1", {FATAL_PUSH}):
        return
    if await pay_tick(c):
        return
    # declare no attackers (keep the game moving)
    if (wf_of(state).get("type") == "DeclareAttackers"
            and state.get("active_player") == 1):
        da = next((x for x in acts if x.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return
    if await scan_interactions(st, "P1", c):
        return
    if not my_priority(state, 1):
        return
    # P1 priority with Brainstorm on stack: respond with Fatal Push
    if (spell_on_stack(state, BRAINSTORM) and not ST["fatal_push_cast"]
            and FATAL_PUSH in hand_names(state, 1)):
        a = next((x for x in acts if x.get("type") == "CastSpell"
                  and obj_name(state, (x.get("data") or {}).get("object_id")) == FATAL_PUSH), None)
        if a:
            say("P1 casts Fatal Push in response to Brainstorm")
            wire("cast_fatal_push", a)
            await submit_as_is(c, a)
            ST["fatal_push_cast"] = True
            return
    if my_main(state, 1):
        for a in acts:
            if a.get("type") == "PlayLand":
                await submit_as_is(c, a)
                return
    for a in acts:
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            return


def load_env(path):
    try:
        with open(path) as f:
            return json.loads(f.read())["state"]
    except Exception:
        return None


def evaluate():
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cast_offered", "A3_alt_cost_paid",
            "A4_exile_answered", "A5_target_prompted", "A6_fow_resolves",
            "A7_no_stall")}
    notes = []
    pre = load_env(f"{EVDIR}/pre.json")
    mid = load_env(f"{EVDIR}/mid_target.json")
    post = load_env(f"{EVDIR}/post.json")
    # A1
    if pre is not None:
        names = [nm for _sid, nm in stack_card_names(pre)]
        ok = (BRAINSTORM in names and FATAL_PUSH in names
              and names.index(BRAINSTORM) < names.index(FATAL_PUSH)
              and pre.get("priority_player") == 0
              and FOW in hand_names(pre, 0) and MURKTIDE in hand_names(pre, 0)
              and find_bf(pre, 0, MEMNITE) is not None
              and life_of(pre, 0) == 20 and life_of(pre, 1) == 20)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"pre.json: stack={names}, pp={pre.get('priority_player')}, "
                     f"P0 hand has FoW={FOW in hand_names(pre,0)} "
                     f"Murktide={MURKTIDE in hand_names(pre,0)}, "
                     f"Memnite on BF={find_bf(pre,0,MEMNITE) is not None}, "
                     f"life={life_of(pre,0)}/{life_of(pre,1)}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json missing (two-spell stack with P0 priority never reached)")
    # A2
    if ST["obs"]["cast_offered"]:
        ass["A2_cast_offered"] = "passed"
        notes.append("CastSpell(Force of Will) advertised to P0 with Brainstorm + Fatal Push on stack")
    else:
        ass["A2_cast_offered"] = "failed"
        notes.append("CastSpell for Force of Will never advertised to P0 with both spells on stack "
                     "-- matches the reported 'can't be cast' symptom")
    # A3
    if post is not None and ST["obs"].get("alt_cost_seen") and life_of(post, 0) == 19:
        ass["A3_alt_cost_paid"] = "passed"
        notes.append("OptionalCostChoice pay=true submitted; P0 life 20 -> 19")
    elif ST["obs"].get("alt_cost_seen"):
        ass["A3_alt_cost_paid"] = "failed"
        notes.append(f"alt-cost choice submitted but P0 life={life_of(post,0) if post else '?'} (want 19)")
    else:
        ass["A3_alt_cost_paid"] = "failed"
        notes.append("no OptionalCostChoice pay=true submission observed")
    # A4
    if post is not None:
        ex = exiled_named(post, MURKTIDE)
        if ST["obs"].get("exile_answered") == MURKTIDE and len(ex) == 1:
            ass["A4_exile_answered"] = "passed"
            notes.append(f"exile prompt offered {ST['obs']['exile_candidates']}; answered {MURKTIDE}; "
                         f"exactly one {MURKTIDE} in Exile")
        else:
            ass["A4_exile_answered"] = "failed"
            notes.append(f"exile_answered={ST['obs'].get('exile_answered')}; "
                         f"Murktide in exile: {len(ex)} (want 1)")
    else:
        ass["A4_exile_answered"] = "failed"
        notes.append("A4 unevaluable (missing post state)")
    # A5
    cands = ST["obs"]["target_candidates"]
    if mid is not None or cands:
        both = FATAL_PUSH in cands and BRAINSTORM in cands
        ass["A5_target_prompted"] = "passed" if both else "failed"
        notes.append(f"FoW TargetSelection candidates: {cands} "
                     f"(want both [{FATAL_PUSH}, {BRAINSTORM}])")
    else:
        ass["A5_target_prompted"] = "failed"
        notes.append("no FoW TargetSelection with stack-spell candidates observed "
                     "(target may have been auto-chosen or never prompted)")
    # A6
    if post is not None:
        ok = (in_gy(post, 1, FATAL_PUSH) and in_gy(post, 0, BRAINSTORM)
              and in_gy(post, 0, FOW)
              and find_bf(post, 0, MEMNITE) is not None
              and life_of(post, 0) == 19 and life_of(post, 1) == 20)
        ass["A6_fow_resolves"] = "passed" if ok else "failed"
        notes.append(f"post.json: FatalPush in P1 GY={in_gy(post,1,FATAL_PUSH)}, "
                     f"Brainstorm in P0 GY={in_gy(post,0,BRAINSTORM)}, "
                     f"FoW in P0 GY={in_gy(post,0,FOW)}, "
                     f"Memnite on BF={find_bf(post,0,MEMNITE) is not None}, "
                     f"life={life_of(post,0)}/{life_of(post,1)}")
    else:
        ass["A6_fow_resolves"] = "failed"
        notes.append("A6 unevaluable (missing post state)")
    # A7
    if post is not None:
        wf = (post.get("waiting_for") or {}).get("type")
        empty = len(post.get("stack") or []) == 0
        pending_fow = "TargetSelection" in json.dumps(post.get("waiting_for") or {}) \
            or "OptionalCostChoice" in json.dumps(post.get("waiting_for") or {})
        if empty and not pending_fow:
            ass["A7_no_stall"] = "passed"
            notes.append(f"post.json: stack empty, waiting_for={wf}; game proceeded")
        else:
            ass["A7_no_stall"] = "failed"
            notes.append(f"post.json: stack_empty={empty} waiting_for={wf}")
    else:
        ass["A7_no_stall"] = "failed"
        notes.append("A7 unevaluable (missing post state)")
    # verdict
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup never assembled: two-spell stack with P0 priority not reached")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    elif (ass["A2_cast_offered"] == "failed" or ass["A3_alt_cost_paid"] == "failed"
          or ass["A4_exile_answered"] == "failed" or ass["A6_fow_resolves"] == "failed"):
        verdict = "reproduced"
        notes.append("FoW alternative-cost cast failed to complete engine-side -- "
                     "matches the reported 'can't be cast' stall")
    else:
        verdict = "blocked"
        notes.append("ambiguous outcome; see assertion detail")
    return verdict, ass, notes


async def finish(reason):
    dur = time.time() - T_START
    if not ST["post_exported"]:
        try:
            post = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            ST["post_exported"] = True
            say("post.json exported at finish()")
        except Exception as e:
            say(f"post export failed: {e}")
    try:
        with open(f"{BACKFILL}/runs/{RUN_ID}/server.log") as f:
            lines = f.readlines()
        tail = [l for l in lines
                if "error" in l.lower() or "panic" in l.lower() or "warn" in l.lower()][-40:]
        with open(f"{EVDIR}/server_excerpt.log", "w") as f:
            f.writelines(tail)
    except Exception as e:
        say(f"server excerpt failed: {e}")
    verdict, ass, notes = evaluate()
    notes.append(f"finish reason: {reason}")
    notes.append("protocol-72 driver: mulligan via advertised MulliganDecision "
                 "as-is (decision=keep, gated on pending[] Declare); bottom via "
                 "SelectCards count from pending[].phase; DiscardToHandSize via "
                 "advertised SelectCards action; OptionalCostChoice pay=true via "
                 "decideOptionalCost value surface; exile via schema select "
                 "(intent=exile, one choiceId); FoW target via schema one-slot "
                 "choice (Fatal Push stack entry); priority-gated passes; "
                 "per-revision action gating; stale-client watchdog.")
    run = {
        "issue": 6301,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(T_START)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "server_port": 9374,
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6301_085.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": ST["obs"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Memnite stands in for the reporter's crewed The Fantasticar as "
            "Fatal Push's victim (documented deviation; the bug is in FoW's "
            "cost flow, not the victim's identity).",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P0 plays exactly 2 lands so 3UU stays unpayable and the "
            "alternative cost is the only path, matching the reporter's board.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0: 12x Force of Will + 10x Brainstorm + 10x Murktide Regent + 8x Memnite + 20x Underground Sea; "
                      "P1: 12x Fatal Push + 48x Underground Sea. P0: T1 Sea+Memnite, T2 Sea, then Brainstorm once "
                      "FoW+Murktide in hand; P1 responds with Fatal Push; P0 responds with Force of Will (alt cost).",
        "contract_line": "FoW alt-cost cast with 2 spells on stack: OptionalCostChoice pay=true (1 life), exile "
                         "Murktide Regent, TargetSelection offering both stack spells -> choose Fatal Push; "
                         "Fatal Push countered, Brainstorm resolves, Memnite survives.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        f.write(json.dumps(run, indent=1))
    WIRE.close()
    RUNLOG.close()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return verdict


async def render_summary(run_path, out_path):
    from PIL import Image, ImageDraw
    run = json.load(open(run_path))
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6301 \u2014 Force of Will alternative-cost cast (revalidation)", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} \u2014 {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), "reporter position: Brainstorm beneath Fatal Push on the stack; alt cost is the only way",
           fill=(140, 160, 180)); y += 30
    vcol = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120)}.get(run["verdict"], (230, 200, 90))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Brainstorm beneath Fatal Push, P0 priority, FoW+Murktide in hand",
        "A2_cast_offered": "A2 CastSpell(Force of Will) advertised with 2 spells on stack",
        "A3_alt_cost_paid": "A3 OptionalCostChoice pay=true; P0 life 20 -> 19",
        "A4_exile_answered": "A4 exile prompt answered with Murktide Regent; Murktide in Exile",
        "A5_target_prompted": "A5 TargetSelection offered BOTH stack spells",
        "A6_fow_resolves": "A6 Fatal Push countered, Brainstorm resolved, Memnite survives",
        "A7_no_stall": "A7 no stuck decision; game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')}  {lab}", fill=col); y += 24
    y += 6
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:9]:
        d.text((40, y), n[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6301/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    global p0, p1, T_START
    T_START = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stall_deadline = None
    stall_note = None
    while time.time() - T_START < 1500:
        await asyncio.sleep(0.25)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            watch(c)
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
                await tick(st, merged_actions(st))
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        # drain rejections
        for c in (p0, p1):
            try:
                while True:
                    t, data = c.inbox.get_nowait()
                    if t in ("ActionRejected", "Error"):
                        ST["obs"]["rejections"].append({"who": c.name, "type": t,
                                                       "data": str(data)[:300]})
                        wire("rejection", {"who": c.name, "type": t, "data": data})
                        say(f"REJECTION [{c.name}]: {str(data)[:300]}")
            except asyncio.QueueEmpty:
                pass
        # completion: everything resolved
        s = p0.latest["state"] if p0.latest else None
        if (s is not None and ST["fow_cast_attempted"]
                and in_gy(s, 0, FOW) and in_gy(s, 1, FATAL_PUSH)
                and in_gy(s, 0, BRAINSTORM) and len(stack_entries(s)) == 0):
            # let the game settle one more beat, then finish
            await asyncio.sleep(3)
            say("all resolved; finishing")
            await finish("all assertions resolvable: FoW + Fatal Push + Brainstorm in graveyards, stack empty")
            await render_summary(f"{EVDIR}/run.json", f"{EVDIR}/summary.png")
            await write_manifest()
            await p0.close(); await p1.close()
            return
        if time.time() - last_diag > 60 and s is not None:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_of(s).get('type')} pp={s.get('priority_player')} "
                f"P0hand={hand_names(s, 0)[:6]} P1hand={hand_names(s, 1)[:6]} "
                f"life={life_of(s, 0)}/{life_of(s, 1)} stack={[n for _, n in stack_card_names(s)]} "
                f"bs={ST['brainstorm_cast']} fp={ST['fatal_push_cast']} fow={ST['fow_cast_attempted']}")
        # FoW never offered: CastSpell(Force of Will) never advertised while P0
        # holds priority with both spells on the stack -> the reported
        # "can't be cast" symptom. Bound the hold so the run terminates.
        if (s is not None and ST["pre_exported"] and not ST["fow_cast_attempted"]
                and ST["obs"].get("no_fow_logged")
                and time.time() - ST.get("no_fow_since", 0) > 120):
            say("FoW CastSpell never advertised in 120s of P0 priority: bug reproduced")
            wire("fow_never_offered", {"waited_s": 120})
            await finish("reproduced: CastSpell(Force of Will) never offered with 2 spells on stack")
            await render_summary(f"{EVDIR}/run.json", f"{EVDIR}/summary.png")
            await write_manifest()
            await p0.close(); await p1.close()
            return
        # stall watchdog: FoW cast attempted but never reached the stack
        if (ST["fow_cast_attempted"] and fow_zone(s) is None and stall_deadline is None
                and s is not None):
            stall_deadline = time.time() + 300
            say("stall watchdog armed (300s): FoW cast attempted, not on stack")
        if stall_deadline and time.time() > stall_deadline and stall_note is None:
            stall_note = (f"STALL: 300s after FoW cast attempt with no FoW on stack; "
                          f"waiting_for={wf_of(s).get('type')}, stack={[n for _, n in stack_card_names(s)]}")
            say(stall_note)
            wire("stall", {"note": stall_note})
            await finish("stall watchdog fired")
            await render_summary(f"{EVDIR}/run.json", f"{EVDIR}/summary.png")
            await write_manifest()
            await p0.close(); await p1.close()
            return
    await finish("global timeout (1500s)")
    await render_summary(f"{EVDIR}/run.json", f"{EVDIR}/summary.png")
    await write_manifest()
    await p0.close(); await p1.close()


asyncio.run(main())
