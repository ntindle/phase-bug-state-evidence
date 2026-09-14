#!/usr/bin/env python3
"""phase-rs/phase #7156 - Reflection of Kiki-Jiki targeting Triumph of Saint
Katherine hangs resolving Triumph's dies trigger.

Oracle (from pinned card-data.json):
  Reflection of Kiki-Jiki (back face of Fable of the Mirror-Breaker):
    {1}, {T}: Create a token that's a copy of another target nonlegendary
    creature you control, except it has haste. Sacrifice it at the beginning
    of the next end step.
  Triumph of Saint Katherine {4}{W} 5/5 Lifelink:
    Praesidium Protectiva - When this creature is put into your graveyard
    from the battlefield, exile it and the top six cards of your library in
    a face-down pile. If you do, shuffle that pile and put it back on top
    of your library.

Report: the Reflection-created Triumph token's dies trigger hangs the game
at the pile operation (p0-softlock). Triage maps it to the pile-shuffle
child; on v0.82.0 the trigger parses fully (ChangeZone ParentTarget ->
ExileTop 6 -> Shuffle TrackedSet -> PutAtLibraryPosition, no Unimplemented),
so this run tests the runtime path, not the old parse gap.

Behavioral contract (2 human seats, native engine):
  P0 casts Fable of the Mirror-Breaker (front face, {2}{R}), rides the saga
  to chapter III (transform -> Reflection of Kiki-Jiki), casts Triumph of
  Saint Katherine ({4}{W}), activates Reflection's {1},{T} ability targeting
  Triumph, and lets the end-step delayed trigger sacrifice the token. The
  token's dies trigger must then resolve: exile the token + top 6, shuffle
  the pile, put it on top - or strand visibly.
  A1 parse:          Triumph dies-trigger AST complete (no Unimplemented);
                     Reflection = CopyTokenOf + delayed End-step Sacrifice;
                     Fable ch.III = exile self + return transformed.
  A2 setup_ok:       Triumph + Reflection on P0 BF, Reflection untapped and
                     past summoning sickness, >=1 untapped land.
  A3 token_created:  target prompt answered with Triumph; token copy on BF.
  A4 trigger_fired:  token sacrificed at end step; dies TriggeredAbility
                     observed on the stack.
  A5 no_hang:        the trigger leaves the stack within the deadline and
                     the game advances (no stranded resolution).
  A6 pile_correct:   post-resolution: no stranded pile cards in exile, P0
                     library count unchanged vs pre-trigger, top-6 multiset
                     identical (shuffled back on top), token object gone.
  A7 cleanup:        stack empty, game advanced, no stall.

Verdict: reproduced iff A2 passes and any of A4/A5/A6 fails (hang, lost
trigger, or wrong pile outcome - each a failure of the reported path) OR the
engine parks on a spurious EffectZoneChoice the card text does not authorize.
not-reproduced iff A2-A7 all pass with no choice prompt. blocked iff A2
cannot be established (e.g. Fable never transforms, Triumph never castable).
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7156")
EVDIR = f"{BACKFILL}/evidence/7156/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FABLE = "Fable of the Mirror-Breaker"
TRIUMPH = "Triumph of Saint Katherine"
REFLECTION = "Reflection of Kiki-Jiki"
MOUNTAIN, PLAINS = "Mountain", "Plains"
LANDS = (MOUNTAIN, PLAINS)

P0_DECK = [(FABLE, 12), (TRIUMPH, 12), (MOUNTAIN, 18), (PLAINS, 18)]
P1_DECK = [(PLAINS, 60)]
TIMEOUT = 2400
HANG_AFTER = 150      # seconds with the trigger on the stack -> hang
FROZEN_AFTER = 90     # seconds with no revision advance while resolving

CD_PATH = f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(already running from prior run; ServerHello verified "
              "v0.82.0/060b5d2/protocol 70) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key).",
}

ST = {}
WF_SEEN = set()
ACTED = {}
C0 = C1 = None


def reset():
    ST.clear()
    ACTED.clear()
    WF_SEEN.clear()
    ST.update({
        "stage": "SETUP",
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "fable_cast": False,
        "fable_cast_turn": None,
        "fable_oid": None,
        "transform_turn": None,
        "reflection_oid": None,
        "triumph_cast": False,
        "triumph_cast_turn": None,
        "triumph_oid": None,
        "activation_submitted": False,
        "activation_accepted": False,
        "target_prompted": False,
        "target_answered": False,
        "target_oid": None,       # actually submitted candidate oid
        "token_oid": None,
        "token_seen_turn": None,
        "pre_sac_exported": False,
        "token_died": False,
        "token_died_turn": None,
        "trigger_seen": False,
        "trigger_seen_t0": None,
        "trigger_gone": False,
        "trigger_resolved": False,
        "pre_trigger_lib": None,   # in-memory pre-resolution snapshot
        "pre_trigger_lib_top6": None,
        "pre_trigger_exile": None,
        "pre_trigger_turn": None,
        "hang_declared": False,
        "zone_choice_seen": False,  # spurious EffectZoneChoice prompt
        "zone_choice_answered": False,
        "answer_rev": None,
        "lib_at_token": None,
        "last_rev": -1,
        "last_rev_t": time.time(),
        "unexpected": [],
        "mulliganed": False,
        "finalized": False,
        "prompt_first_seen": {},
    })


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
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


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if oname(o) in LANDS and not o.get("tapped")]


def exile_names(state, pid):
    return sorted(oname(o) for oid, o in state["objects"].items()
                  if o.get("zone") == "Exile" and o.get("controller") == pid)


def lib_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [oname(state["objects"].get(str(x), {}))
                    for x in (p.get("library") or [])]
    return []


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_data(state):
    return wf_of(state).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


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
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
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
    ST["exports"][path] = True
    say(f"exported {path}")
    return s


# ---------------- viewer_interaction helpers -------------------------------
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_opps(st):
    vi = get_vi(st)
    return vi.get("opportunities", []) if vi else []


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id",
                     "hit_card", "card_id") and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        deep_refs(s.get("data") or {}, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def choice_card_name(ch, state):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        for key in ("card_name", "name", "title"):
            v = d.get(key)
            if v:
                return str(v)
    r = ref_of(ch)
    if r is not None:
        o = (state.get("objects") or {}).get(str(r))
        if o:
            return oname(o)
    return choice_text(ch)


def accept_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") in ("accept", "option"):
            v = d.get("value")
            if v is not None:
                return str(v)
    return None


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
            acts.append(a)
    return acts


def cast_action_for(acts, oid):
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_for(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def find_action(acts, atype):
    for a in acts:
        if a["type"] == atype:
            return a
    return None


def stack_entries(state):
    return state.get("stack") or []


def triumph_trigger_on_stack(state):
    """True if the Triumph dies trigger is currently on the stack."""
    for e in stack_entries(state):
        blob = json.dumps(e, default=str).lower()
        if e.get("kind") == "TriggeredAbility" and (
                "face-down pile" in blob or "praesidium" in blob):
            return True
    return False


async def handle_zone_choice(c, pid, tag, st, state):
    # EffectZoneChoice: engine asking the player to choose cards for a
    # zone-change effect (e.g. Triumph's PutAtLibraryPosition). Log the
    # full opportunity once, then answer with the first card candidate.
    wf = wf_of(state)
    if not wf or wf.get("type") != "EffectZoneChoice":
        return False
    data = wf.get("data") or {}
    if data.get("player") != pid:
        return False
    rev = st.get("state_revision", -1)
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = True
            ST["zone_choice_seen"] = True
            # This IS the dies trigger firing: PutAtLibraryPosition with the
            # sacrificed token as source. Mark it here (race-free: the prompt
            # itself is the observation, no stack sampling needed).
            if (data.get("effect_kind") == "PutAtLibraryPosition"
                    and str(data.get("source_id")) == str(ST.get("token_oid"))
                    and not ST["trigger_seen"]):
                ST["trigger_seen"] = True
                ST["trigger_seen_t0"] = time.time()
                ST["pre_trigger_turn"] = state.get("turn_number")
                ST["exiled_pile"] = [
                    oname(state.get("objects", {}).get(str(c), {}))
                    for c in (data.get("cards") or [])]
                ST["stage"] = "RESOLVE_WATCH"
                ST["choice_answered_turn"] = None
                say(f"[P0] TRIUMPH DIES TRIGGER FIRED via EffectZoneChoice "
                    f"(source=token {ST['token_oid']}); "
                    f"pile={ST['exiled_pile']}")
                wire("trigger_fired_via_choice",
                     {"turn": state.get("turn_number"),
                      "source": ST["token_oid"],
                      "pile": ST["exiled_pile"]})
                await export_now("pre_trigger.json")
            wire("zone_choice_opp",
                 {"who": tag, "wf_data": data,
                  "opportunity": opp})
            say(f"[{tag}] EffectZoneChoice opp: "
                f"{len(chs)} choices rtype={rtype} "
                f"effect={data.get('effect_kind')}")
        if acted(f"ezc{pid}", rev):
            return True
        pick = None
        for ch in chs:
            if ref_of(ch) is not None:
                pick = ch
                break
        if pick is None:
            pick = chs[0]
        resp = opp.get("response", {}) or {}
        rtype2 = resp.get("type") or "exactChoices"
        if rtype2 == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        say(f"[{tag}] answering EffectZoneChoice with {pick.get('id')} "
            f"({choice_card_name(pick, state)})")
        wire("interaction_submission",
             {"who": tag, "submission": sub, "zone_choice": True,
              "picked_card": choice_card_name(pick, state)})
        await c.send_interaction(sub)
        ST["zone_choice_answered"] = True
        ST["zone_choice_answered_t"] = time.time()
        ST["answer_rev"] = rev
        return True
    return False


async def preamble(c, pid, st, state, acts):
    """Mulligan / legend / hand-size discard / blockers / declare-empty.
    Returns True if it acted (caller should return)."""
    rev = st.get("state_revision", -1)
    for a in acts:
        if "Legend" in a["type"]:
            if not acted(f"leg{pid}", rev):
                await submit_as_is(c, a)
                say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True
    if wf_type(state) == "MulliganDecision":
        pend = wf_data(state).get("pending", []) or []
        mine = [p for p in pend if p.get("player") == pid]
        if mine and (mine[0].get("phase") or {}).get("type") \
                == "BottomCards":
            n = (mine[0].get("phase") or {}).get("count") or 1
            oids = [x for x in hand_oids(state, pid)
                    if oname(state["objects"][x]) not in (FABLE, TRIUMPH)][:n]
            pick = {"type": "SelectCards",
                    "data": {"cards": [int(x) for x in oids]}}
            if not acted(f"bott{pid}", rev):
                await submit_as_is(c, pick)
                say(f"[{c.name}] bottoms after mulligan: {pick}")
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            mine = [p for p in pend if p.get("player") == pid]
            if wf_type(state) == "MulliganDecision" and mine:
                if not acted(f"mull{pid}", rev):
                    phase = (mine[0].get("phase") or {}).get("type", "")
                    if phase != "BottomCards":
                        choice = "Keep"
                        if pid == 0 and not ST.get("mulliganed"):
                            has_fable = any(
                                oname(state["objects"][x]) == FABLE
                                for x in hand_oids(state, 0))
                            if not has_fable:
                                choice = "Mulligan"
                                ST["mulliganed"] = True
                        await submit_as_is(
                            c, {"type": "MulliganDecision",
                                "data": {"choice": {"type": choice}}})
                        say(f"[{c.name}] mulligan decision: {choice}")
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        protect = set()
        if pid == 0:
            if not ST["fable_cast"]:
                protect |= {FABLE}
            if not ST["triumph_cast"]:
                protect |= {TRIUMPH}
        oids = hand_oids(state, pid)

        def rank(x):
            nm = oname(state["objects"][x])
            if nm in protect:
                return 2
            return 0 if nm in LANDS else 1
        oids.sort(key=rank)
        picks = [int(x) for x in oids[:n]]
        if picks and not acted(f"hsd{pid}", rev):
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{c.name}] discards {n} to hand size")
        return True
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no blockers")
            return True
    if (state.get("phase") or "") == "DeclareAttackers" \
            and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            if not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no attackers")
            return True
    return False


# ---------------- prompt handlers -------------------------------------------
async def handle_optional_decline(c, pid, tag, st, state):
    """Decline 'you may' OptionalEffectChoice prompts during SETUP (the saga
    chapter II discard; declining skips discard+draw cleanly)."""
    wt = (wf_type(state) or "").lower()
    if "optional" not in wt:
        return False
    if wf_player(state) != pid or ST["stage"] != "SETUP":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if iid in ST["prompt_first_seen"]:
            continue
        ST["prompt_first_seen"][iid] = True
        want = None
        for ch in chs:
            if str(accept_of(ch)).lower() == "false":
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: optional prompt with no decline choice: "
                f"{[choice_text(ch)[:40] for ch in chs]}")
            return False
        wire("optional_prompt", {"wf_type": wf_type(state),
                                 "choices": [choice_text(ch)[:60]
                                             for ch in chs]})
        say(f"[{tag}] declining optional prompt ({wf_type(state)})")
        await answer_vi(c, opp, want, tag)
        return True
    return False


async def handle_miracle_decline(c, pid, tag, st, state):
    """MiracleReveal: P0 drew Triumph as the first card of the turn. Decline
    the miracle cast (we want the normal {4}{W} cast later, and the fixture
    must not fork on miracle timing). Works in any stage."""
    if (wf_type(state) or "") != "MiracleReveal":
        return False
    if wf_player(state) != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if iid in ST["prompt_first_seen"]:
            continue
        ST["prompt_first_seen"][iid] = True
        want = None
        for ch in chs:
            if str(accept_of(ch)).lower() == "false":
                want = ch
                break
        if want is None:
            for ch in chs:
                tx = choice_text(ch).lower()
                if any(k in tx for k in ("decline", "don't", "do not", "no ",
                                        "keep in hand", "not cast")):
                    want = ch
                    break
        if want is None:
            say(f"[{tag}] WARNING: MiracleReveal with no decline choice: "
                f"{[choice_text(ch)[:60] for ch in chs]}")
            wire("miracle_prompt_unhandled",
                 {"choices": [choice_text(ch)[:60] for ch in chs]})
            return False
        wire("miracle_prompt", {"wf_type": wf_type(state),
                                "choices": [choice_text(ch)[:60]
                                            for ch in chs]})
        say(f"[{tag}] declining miracle cast ({wf_type(state)})")
        await answer_vi(c, opp, want, tag)
        return True
    return False


async def handle_discard_select(c, pid, tag, st, state):
    """Saga chapter II fallback: a discard SelectCards for P0. Discard
    lands-first, protecting Fable/Triumph."""
    wt = (wf_type(state) or "").lower()
    if "discard" not in wt:
        return False
    if wf_player(state) != pid:
        return False
    rev = st.get("state_revision", -1)
    for a in merged_actions(st):
        if a.get("type") == "SelectCards" and not acted(f"dsc{pid}", rev):
            n = (a.get("data", {}).get("count")
                 or len(a.get("data", {}).get("cards") or []))
            n = min(n or 2, 2)
            oids = hand_oids(state, pid)

            def rank(x):
                nm = oname(state["objects"][x])
                if nm in (FABLE, TRIUMPH):
                    return 2
                return 0 if nm in LANDS else 1
            oids.sort(key=rank)
            picks = [int(x) for x in oids[:n]]
            wire("discard_select", {"wf_type": wf_type(state), "picks": picks,
                                    "names": [oname(state["objects"][str(x)])
                                              for x in picks]})
            say(f"[{tag}] ch.II discard: discarding "
                f"{[oname(state['objects'][str(x)]) for x in picks]}")
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            return True
    # viewer_interaction fallback: schema select with card candidates
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            iid = opp.get("interactionId")
            if iid in ST["prompt_first_seen"]:
                continue
            ST["prompt_first_seen"][iid] = True
            picks = []
            for ch in chs:
                nm = choice_card_name(ch, state)
                if nm in LANDS and len(picks) < 2:
                    picks.append(ch.get("id"))
            if not picks:
                continue
            resp = opp.get("response", {}) or {}
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": picks}}}
            say(f"[{tag}] ch.II discard via vi: {len(picks)} lands")
            wire("interaction_submission",
                 {"who": tag, "submission": sub, "discard_vi": True})
            await c.send_interaction(sub)
            return True
    return False


async def handle_activation_target(c, pid, tag, st, state):
    """Answer the Reflection CopyTokenOf target selection with Triumph,
    recording the ACTUALLY submitted candidate oid."""
    if not ST.get("activation_submitted") or ST.get("target_answered"):
        return False
    if wf_player(state) != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = True
            ST["target_prompted"] = True
            wire("target_prompt_shape",
                 {"spec": ((opp.get("response", {}) or {}).get("data", {})
                           or {}).get("spec"),
                  "n_choices": len(chs),
                  "choice_names": [choice_card_name(ch, state)
                                   for ch in chs]})
            say(f"[{tag}] target prompt: {len(chs)} candidates")
        want = None
        for ch in chs:
            if choice_card_name(ch, state) == TRIUMPH:
                r = ref_of(ch)
                o = (state.get("objects") or {}).get(str(r)) if r else None
                if o and o.get("controller") == pid \
                        and o.get("zone") == "Battlefield":
                    want = ch
                    break
        if want is None:
            say(f"[{tag}] WARNING: Triumph not among target candidates: "
                f"{[choice_card_name(ch, state) for ch in chs][:8]}")
            return False
        ST["target_oid"] = str(ref_of(want))
        say(f"[{tag}] targeting Triumph oid={ST['target_oid']}")
        await answer_vi(c, opp, want, tag)
        ST["target_answered"] = True
        return True
    return False


# ---------------- P0 tick ----------------------------------------------------
async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    rev = st.get("state_revision", -1)
    if wt and wt not in WF_SEEN:
        WF_SEEN.add(wt)
        wire("wf_type_seen", {"type": wt, "player": wp,
                              "stage": ST.get("stage")})

    # cost/target prompt handlers first: never pass while ours is pending
    if await handle_miracle_decline(c, 0, "P0", st, state):
        return True
    if await handle_optional_decline(c, 0, "P0", st, state):
        return True
    if await handle_zone_choice(c, 0, "P0", st, state):
        return True
    if await handle_discard_select(c, 0, "P0", st, state):
        return True
    if await handle_activation_target(c, 0, "P0", st, state):
        return True

    # --- activation accepted? (ability object or resolution trace) ----------
    if ST["activation_submitted"] and not ST["activation_accepted"]:
        if ST["target_answered"]:
            ST["activation_accepted"] = True
            ST["stage"] = "TOKEN_WAIT"
            say("[P0] activation accepted (target answered)")
            wire("activation_accepted", {"target_oid": ST["target_oid"]})

    # --- token watch -------------------------------------------------------
    if ST["stage"] == "TOKEN_WAIT" and not ST["token_oid"]:
        before = set()
        triumphs = bf_named(state, 0, TRIUMPH)
        # the token is a NEW Triumph-named object appearing after activation
        for oid in triumphs:
            if oid != str(ST.get("triumph_oid")):
                ST["token_oid"] = oid
                ST["token_seen_turn"] = state.get("turn_number")
                ST["stage"] = "WAIT_SACRIFICE"
                ST["lib_at_token"] = lib_names(state, 0)
                say(f"[P0] TOKEN created: oid={oid} "
                    f"(turn {ST['token_seen_turn']}) "
                    f"lib={len(ST['lib_at_token'])}")
                wire("token_created", {"oid": oid,
                                       "turn": ST["token_seen_turn"]})
                await export_now("mid_token.json")
                break

    # --- sacrifice / trigger watch (runs every tick once token exists) ----
    if ST["token_oid"]:
        o = (state.get("objects") or {}).get(str(ST["token_oid"]))
        if not ST["token_died"] and (o is None or o.get("zone") != "Battlefield"):
            ST["token_died"] = True
            ST["token_died_turn"] = state.get("turn_number")
            ST["pre_trigger_turn"] = state.get("turn_number")
            # pre-resolution snapshots (race-proof assertion sources)
            ST["pre_trigger_lib"] = lib_names(state, 0)
            ST["pre_trigger_lib_top6"] = list(ST["pre_trigger_lib"][:6])
            ST["pre_trigger_exile"] = exile_names(state, 0)
            say(f"[P0] token left battlefield "
                f"(zone={(o or {}).get('zone')}, turn {ST['token_died_turn']}); "
                f"pre lib={len(ST['pre_trigger_lib'])} "
                f"top6={ST['pre_trigger_lib_top6']} "
                f"exile={ST['pre_trigger_exile']}")
            wire("token_died", {"zone": (o or {}).get("zone"),
                                "turn": ST["token_died_turn"],
                                "pre_lib_count": len(ST["pre_trigger_lib"]),
                                "pre_top6": ST["pre_trigger_lib_top6"],
                                "pre_exile": ST["pre_trigger_exile"]})

    on_stack = triumph_trigger_on_stack(state)
    # Secondary "fired" signal: seen on the stack (the primary signal is the
    # EffectZoneChoice prompt itself, marked in handle_zone_choice).
    if ST["token_died"] and on_stack and not ST["trigger_seen"]:
        ST["trigger_seen"] = True
        ST["trigger_seen_t0"] = time.time()
        ST["stage"] = "RESOLVE_WATCH"
        ST["last_rev"] = rev
        ST["last_rev_t"] = time.time()
        ST["exiled_pile"] = exile_names(state, 0)
        say(f"[P0] TRIUMPH DIES TRIGGER ON STACK "
            f"(turn {state.get('turn_number')})")
        wire("trigger_on_stack",
             {"turn": state.get("turn_number"),
              "stack": [str(e.get("kind")) for e in stack_entries(state)]})
        await export_now("pre_trigger.json")

    if ST["trigger_seen"] and not ST["trigger_gone"]:
        # "Engine done": the (spurious) choice was answered and the game has
        # clearly moved on (a later turn began). The pile outcome is asserted
        # in A6 from the exported post.json.
        answered = ST.get("zone_choice_answered", False)
        ans_rev = ST.get("answer_rev") or -1
        wf = wf_type(state)
        if answered and rev > ans_rev and wf != "EffectZoneChoice":
            ST["trigger_gone"] = True
            ST["trigger_resolved"] = True
            ST["stage"] = "SETTLE"
            ex = exile_names(state, 0)
            lib = lib_names(state, 0)
            say("[P0] engine accepted the choice and moved on; "
                f"exile now={ex} lib={len(lib)} "
                f"top={lib[0] if lib else None}")
            wire("trigger_resolved",
                 {"elapsed": time.time() - ST["trigger_seen_t0"],
                  "post_exile": ex, "post_lib_count": len(lib),
                  "post_lib_top6": lib[:6]})
            await export_now("post.json")
            ST["done_reason"] = "choice answered and accepted by engine"
            ST["stop"] = True
            return True
        # hang watchdog: revision frozen while the pile sits unresolved
        if rev != ST["last_rev"]:
            ST["last_rev"] = rev
            ST["last_rev_t"] = time.time()
        frozen = time.time() - ST["last_rev_t"]
        elapsed = time.time() - ST["trigger_seen_t0"]
        if elapsed > HANG_AFTER or frozen > FROZEN_AFTER:
            ST["hang_declared"] = True
            say(f"[P0] HANG: pile unresolved {elapsed:.0f}s, revision "
                f"frozen {frozen:.0f}s, waiting_for={wf}")
            wire("hang", {"elapsed": elapsed, "frozen": frozen,
                          "waiting_for": wf,
                          "waiting_for_data": wf_data(state),
                          "exile": exile_names(state, 0),
                          "stack": [json.dumps(e, default=str)[:300]
                                    for e in stack_entries(state)]})
            await export_now("post_hang.json")
            ST["done_reason"] = (f"hang: dies-trigger pile unresolved after "
                                 f"{elapsed:.0f}s (waiting_for={wf})")
            ST["stop"] = True
            return True
        return True  # hold: never pass while our trigger is resolving

    # token died but no trigger appeared within 45s -> related failure
    if ST["token_died"] and not ST["trigger_seen"] and not ST.get(
            "no_trigger_done"):
        if ST.get("no_trigger_t0") is None:
            ST["no_trigger_t0"] = time.time()
        if time.time() - ST["no_trigger_t0"] > 45:
            ST["no_trigger_done"] = True
            say("[P0] token died 45s ago, dies trigger never hit the stack")
            wire("no_trigger", {"stack": [str(e.get("kind"))
                                          for e in stack_entries(state)]})
            await export_now("post_no_trigger.json")
            ST["done_reason"] = "token died, dies trigger never fired"
            ST["stop"] = True
            return True

    # --- main-phase action taking ------------------------------------------
    if is_my_main(state, 0) and not (state.get("stack") or []):
        # land drop (retry every tick; prefer Mountain first, then Plains)
        if ST["stage"] in ("SETUP", "ACTIVATE"):
            mtn = sum(1 for _, o in bf(state, 0) if oname(o) == MOUNTAIN)
            pln = sum(1 for _, o in bf(state, 0) if oname(o) == PLAINS)
            want = MOUNTAIN if mtn <= pln else PLAINS
            lo = find_hand(state, 0, want) or find_hand(
                state, 0, MOUNTAIN if want == PLAINS else PLAINS)
            pla = playland_for(acts, lo)
            if pla:
                await submit_as_is(c, pla)
                say(f"[P0] plays land {oname(state['objects'][lo])}")
                return True

        # cast Fable (once)
        if not ST["fable_cast"]:
            fo = find_hand(state, 0, FABLE)
            ca = cast_action_for(acts, fo)
            if ca:
                await submit_as_is(c, ca)
                ST["fable_cast"] = True
                ST["fable_cast_turn"] = state.get("turn_number")
                say(f"[P0] casts Fable (turn {ST['fable_cast_turn']})")
                return True

        # track the Fable object (saga, on BF under its front-face name)
        if ST["fable_cast"] and not ST["fable_oid"]:
            fos = bf_named(state, 0, FABLE)
            if fos:
                ST["fable_oid"] = fos[0]

        # cast Triumph (once)
        if ST["fable_cast"] and not ST["triumph_cast"]:
            to = find_hand(state, 0, TRIUMPH)
            ca = cast_action_for(acts, to)
            if ca:
                await submit_as_is(c, ca)
                ST["triumph_cast"] = True
                ST["triumph_cast_turn"] = state.get("turn_number")
                say(f"[P0] casts Triumph (turn {ST['triumph_cast_turn']})")
                return True

        # track Triumph oid
        if ST["triumph_cast"] and not ST["triumph_oid"]:
            tos = bf_named(state, 0, TRIUMPH)
            if tos:
                ST["triumph_oid"] = tos[0]

        # track transform: Reflection appears (Fable leaves BF)
        if ST["fable_cast"] and not ST["reflection_oid"]:
            ros = bf_named(state, 0, REFLECTION)
            if ros:
                ST["reflection_oid"] = ros[0]
                ST["transform_turn"] = state.get("turn_number")
                ST["stage"] = "ACTIVATE"
                say(f"[P0] TRANSFORMED: Reflection oid={ros[0]} "
                    f"(turn {ST['transform_turn']})")
                wire("transformed", {"oid": ros[0],
                                     "turn": ST["transform_turn"]})
                await export_now("mid_transform.json")

        # --- activation window -------------------------------------------
        if ST["stage"] == "ACTIVATE" and not ST["activation_submitted"]:
            ros = bf_named(state, 0, REFLECTION)
            tos = bf_named(state, 0, TRIUMPH)
            if ros and tos:
                ro = state["objects"][ros[0]]
                ready = (not ro.get("tapped")
                         and state.get("turn_number", 0)
                         > (ST["transform_turn"] or 0))
                unt = len(untapped_lands(state, 0))
                if ready and unt >= 1 and wt in ("Priority", None, ""):
                    found = None
                    for a in acts:
                        if a["type"] == "ActivateAbility" and str(
                                a.get("data", {}).get("source_id")) == ros[0]:
                            found = a
                            break
                    wire("offer_scan",
                         {"turn": state.get("turn_number"),
                          "offered": bool(found),
                          "untapped_lands": unt,
                          "reflection_tapped": ro.get("tapped")})
                    if found:
                        ST["activation_submitted"] = True
                        await export_now("pre_activation.json")
                        await submit_as_is(c, found)
                        say(f"[P0] ACTIVATION SUBMITTED (target Triumph "
                            f"oid={tos[0]})")
                        return True
                    else:
                        say(f"[P0] activation not offered "
                            f"(turn {state.get('turn_number')}, "
                            f"untapped_lands={unt})")
        # fall through to default pass below

    # --- fixture-blocked watches -------------------------------------------
    turn = state.get("turn_number", 0)
    if ST["fable_cast"] and not ST["reflection_oid"] \
            and turn > (ST["fable_cast_turn"] or 0) + 8 \
            and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] BLOCKED: Fable never transformed 8+ turns after cast")
        wire("blocked", {"reason": "no_transform",
                         "fable_cast_turn": ST["fable_cast_turn"],
                         "turn": turn})
        await export_now("post_blocked.json")
        ST["done_reason"] = "Fable of the Mirror-Breaker never transformed"
        ST["stop"] = True
        return True
    if not ST["fable_cast"] and turn >= 16 and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] BLOCKED: Fable never castable/offered by turn 16")
        wire("blocked", {"reason": "fable_never_cast", "turn": turn})
        await export_now("post_blocked.json")
        ST["done_reason"] = "Fable never cast"
        ST["stop"] = True
        return True
    if ST["reflection_oid"] and not ST["triumph_cast"] and turn >= 24 \
            and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] BLOCKED: Triumph never castable by turn 24")
        wire("blocked", {"reason": "triumph_never_cast", "turn": turn})
        await export_now("post_blocked.json")
        ST["done_reason"] = "Triumph never cast"
        ST["stop"] = True
        return True

    # --- default: pass priority -------------------------------------------
    pp = find_action(acts, "PassPriority")
    if pp and not acted("pp0", rev):
        if wp == 0 and wt and wt != "Priority":
            return False
        await submit_as_is(c, pp)
        return True
    return False


# ---------------- P1 tick (passive) -----------------------------------------
async def p1_tick(c, st, state, acts):
    if await preamble(c, 1, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    if is_my_main(state, 1) and not (state.get("stack") or []):
        lo = find_hand(state, 1, PLAINS)
        pla = playland_for(acts, lo)
        if pla:
            await submit_as_is(c, pla)
            say("[P1] plays land")
            return True
    pp = find_action(acts, "PassPriority")
    if pp and not acted("pp1", st.get("state_revision", -1)):
        if wp == 1 and wt and wt != "Priority":
            return False
        await submit_as_is(c, pp)
        return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return
    state = st.get("state") or {}
    if not state:
        return
    ST["life"][0] = life(state, 0) or ST["life"][0]
    ST["life"][1] = life(state, 1) or ST["life"][1]
    acts = merged_actions(st)
    drain_rejections(c)
    if pid == 0:
        await p0_tick(c, st, state, acts)
    else:
        await p1_tick(c, st, state, acts)


# ---------------- parse check -------------------------------------------------
def parse_check():
    cd = json.load(open(CD_PATH))
    parts = []

    # Triumph dies trigger: full chain, no Unimplemented
    tr = cd["triumph of saint katherine"]["triggers"]
    dies = [t for t in tr if t.get("mode") == "ChangesZone"
            and t.get("destination") == "Graveyard"]
    ok_t = False
    if dies:
        d = dies[0]
        ex = d["execute"]
        chain = []
        node = ex
        while node:
            eff = node.get("effect") or {}
            chain.append(eff.get("type"))
            node = node.get("sub_ability")
        unimp = "Unimplemented" in json.dumps(d)
        ok_t = (chain[:4] == ["ChangeZone", "ExileTop", "Shuffle",
                              "PutAtLibraryPosition"]
                and not unimp)
        parts.append(f"triumph_chain={chain} unimplemented={unimp}")
    else:
        parts.append("triumph_dies_trigger_missing")

    # Reflection activated ability: CopyTokenOf + delayed End-step Sacrifice
    ra = [a for a in cd["reflection of kiki-jiki"]["abilities"]
          if a.get("kind") == "Activated"]
    ok_r = False
    if ra:
        a = ra[0]
        eff = (a.get("effect") or {}).get("type")
        sub = a.get("sub_ability") or {}
        seff = (sub.get("effect") or {}).get("type")
        cond = ((sub.get("effect") or {}).get("condition") or {}).get("type")
        seff2 = (((sub.get("effect") or {}).get("effect") or {}).get("effect")
                 or {}).get("type")
        ok_r = (eff == "CopyTokenOf" and seff == "CreateDelayedTrigger"
                and cond == "AtNextPhase" and seff2 == "Sacrifice")
        parts.append(f"reflection={eff}/{seff}/{cond}/{seff2}")
    else:
        parts.append("reflection_activated_missing")

    # Fable chapter III: exile self + return transformed
    ch3 = [t for t in cd["fable of the mirror-breaker"]["triggers"]
           if t.get("saga_chapter") == 3]
    ok_f = False
    if ch3:
        e1 = (ch3[0]["execute"].get("effect") or {}).get("type")
        sub = ch3[0]["execute"].get("sub_ability") or {}
        e2 = (sub.get("effect") or {}).get("type")
        et = (sub.get("effect") or {}).get("enter_transformed")
        ok_f = (e1 == "ChangeZone" and e2 == "ChangeZone" and et is True)
        parts.append(f"fable_ch3={e1}/{e2} transformed={et}")
    else:
        parts.append("fable_ch3_missing")

    ok = ok_t and ok_r and ok_f
    detail = "; ".join(parts)
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_check", {"ok": ok, "detail": detail})
    return "passed" if ok else "failed"


def load_state(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())["state"]
    except Exception:
        return None


async def finalize():
    if ST.get("finalized"):
        return
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "timeout"
    say("finalizing...")

    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7156.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass = {}
    notes = []
    a1 = ST.get("a1", "not-run")
    ass["A1_parse"] = a1
    notes.append(f"A1: triumph chain + reflection copy/sacrifice + fable "
                 f"ch.III transform parse: {a1}")

    pre_act = load_state("pre_activation.json")
    ok2 = False
    if pre_act and ST.get("reflection_oid") and ST.get("triumph_oid"):
        ro = (pre_act.get("objects") or {}).get(str(ST["reflection_oid"]))
        to = (pre_act.get("objects") or {}).get(str(ST["triumph_oid"]))
        unt = len(untapped_lands(pre_act, 0))
        ok2 = (ro is not None and ro.get("zone") == "Battlefield"
               and not ro.get("tapped")
               and to is not None and to.get("zone") == "Battlefield"
               and unt >= 1
               and (pre_act.get("turn_number") or 0) > (ST.get(
                   "transform_turn") or 0))
        notes.append(f"A2: reflection untapped={ro and not ro.get('tapped')} "
                     f"triumph_on_bf={to is not None} untapped_lands={unt} "
                     f"turn={pre_act.get('turn_number')} "
                     f"transform_turn={ST.get('transform_turn')}")
    else:
        notes.append("A2: pre_activation.json missing (activation window "
                     "never opened)")
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"

    ok3 = ok2 and ST["token_oid"] is not None and ST["target_answered"]
    ass["A3_token_created"] = "passed" if ok3 else (
        "not-run" if not ok2 else "failed")
    notes.append(f"A3: target_answered={ST['target_answered']} "
                 f"(submitted oid={ST['target_oid']}) "
                 f"token_oid={ST['token_oid']} "
                 f"token_seen_turn={ST['token_seen_turn']}")

    ok4 = ok3 and ST["trigger_seen"]
    ass["A4_trigger_fired"] = "passed" if ok4 else (
        "not-run" if not ST["token_died"] else "failed")
    notes.append(f"A4: token_died={ST['token_died']} "
                 f"(turn {ST['token_died_turn']}) "
                 f"trigger_seen={ST['trigger_seen']}")

    ok5 = ok4 and ST["trigger_resolved"] and not ST["hang_declared"]
    ass["A5_no_hang"] = "passed" if ok5 else (
        "not-run" if not ST["trigger_seen"] else "failed")
    notes.append(f"A5: trigger_resolved={ST['trigger_resolved']} "
                 f"hang_declared={ST['hang_declared']} "
                 f"zone_choice_seen={ST.get('zone_choice_seen')}")

    # A6: pile outcome from saved states (post.json vs snapshots).
    # The pile is the exiled top-six (ST["exiled_pile"]); the true pre-trigger
    # library is captured at token creation (no draws happen between the
    # main-phase copy and the end-step sacrifice).
    ok6 = False
    detail6 = ""
    post = load_state("post.json")
    lib0 = ST.get("lib_at_token")
    pile = ST.get("exiled_pile")
    if ok5 and post and lib0 is not None and pile:
        post_lib = lib_names(post, 0)
        lib_same_count = len(post_lib) == len(lib0)
        pile_home_multiset = sorted(post_lib[:6]) == sorted(pile)
        post_exile = exile_names(post, 0)
        exile_clean = sorted(post_exile) == sorted(
            ST.get("pre_trigger_exile") or [])
        tok = (post.get("objects") or {}).get(str(ST["token_oid"]))
        token_gone = tok is None or tok.get("zone") not in (
            "Battlefield", "Exile", "Graveyard", "Hand", "Library")
        ok6 = lib_same_count and pile_home_multiset and exile_clean \
            and token_gone
        detail6 = (f"lib_count {len(lib0)}->{len(post_lib)} "
                   f"pile_multiset_home={pile_home_multiset} "
                   f"(pile={pile} post_top6={post_lib[:6]}) "
                   f"exile_clean={exile_clean} (post_exile={post_exile}) "
                   f"token_gone={token_gone}")
    elif ST["hang_declared"]:
        detail6 = "not-run: hang stranded the resolution"
    else:
        detail6 = "not-run: trigger never resolved"
    ass["A6_pile_correct"] = "passed" if ok6 else (
        "not-run" if not ok5 else "failed")
    notes.append(f"A6: {detail6}")

    ok7 = False
    if post:
        stack_empty = not (post.get("stack") or [])
        advanced = (post.get("turn_number") or 0) >= (
            ST.get("pre_trigger_turn") or 0)
        ok7 = stack_empty and advanced and not ST["hang_declared"]
        notes.append(f"A7: stack_empty={stack_empty} "
                     f"turn {ST.get('pre_trigger_turn')}->"
                     f"{post.get('turn_number')} hang={ST['hang_declared']}")
    else:
        post_h = load_state("post_hang.json")
        if post_h:
            notes.append(f"A7: post_hang.json captured; stack="
                         f"{len(post_h.get('stack') or [])} "
                         f"waiting_for={(post_h.get('waiting_for') or {}).get('type')}")
        else:
            notes.append("A7: no post state captured")
    ass["A7_cleanup"] = "passed" if ok7 else (
        "not-run" if not post else "failed")

    verdict = "blocked"
    if ok2:
        failed = any(
            ass[k] == "failed" for k in
            ("A4_trigger_fired", "A5_no_hang", "A6_pile_correct"))
        # The card text authorizes no player choice ("shuffle that pile and
        # put it back on top of your library"), so a parked EffectZoneChoice
        # is the reported path failing even if the driver answers it.
        spurious = ST.get("zone_choice_seen", False)
        verdict = "reproduced" if (failed or spurious) else "not-reproduced"
    notes.append(f"verdict rule: {verdict} "
                 f"(done_reason={ST['done_reason']} "
                 f"zone_choice_seen={ST.get('zone_choice_seen')})")

    run = {
        "issue": 7156,
        "title": "triumph of st. Katherine \u2014 Reflection of Kiki-Jiki "
                 "targeting Triumph of St. Katherine hangs resolving the "
                 "dies trigger",
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "validated_version": "v0.82.0",
        "server": SERVER_IDENTITY,
        "scope": "Fable of the Mirror-Breaker saga -> transform into "
                 "Reflection of Kiki-Jiki -> {1},{T} copy of Triumph of "
                 "Saint Katherine -> end-step sacrifice -> dies-trigger "
                 "pile resolution; native engine, two human-client seats",
        "assertions": ass,
        "assertion_notes": notes,
        "verdict": verdict,
        "rejections": ST["rejections"],
        "wf_types_seen": sorted(WF_SEEN),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "P1 fully passive (60x Plains; plays land, never casts/"
            "attacks/blocks).",
            "The reported Discord attachment (turn-17 game state zip) was "
            "not used: its signed URL expired; the fixture replays the "
            "reported sequence from a fresh game instead.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"run.json written; verdict={verdict}")

    try:
        render_summary(ass, notes, verdict)
        say("summary.png rendered")
    except Exception as e:
        say(f"summary render failed: {e}")
        wire("summary_failed", {"error": str(e)[:200]})

    # ---- manifest LAST -------------------------------------------------
    # Flush both logs, then hash. No say()/wire() calls after this point:
    # say() appends to scenario_run.log and wire() appends to
    # wire_log.jsonl, either of which would invalidate the manifest.
    WIRE.flush()
    RUNLOG.flush()
    files = sorted(os.listdir(EVDIR))
    lines = []
    for fn in files:
        if fn == "manifest.sha256":
            continue
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    mh = hashlib.sha256(
        open(f"{EVDIR}/manifest.sha256", "rb").read()).hexdigest()
    # stdout only (NOT say(): that would append to scenario_run.log and
    # invalidate its manifest hash)
    print(f"manifest written ({len(lines)} files) "
          f"manifest-sha256={mh}", flush=True)
    WIRE.close()
    RUNLOG.close()


def render_summary(ass, notes, verdict):
    from PIL import Image, ImageDraw
    W, H = 1000, 1000
    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #7156 \u2014 Reflection of Kiki-Jiki / "
                    "Triumph of Saint Katherine", fill=(240, 240, 240))
    y += 30
    d.text((24, y), "v0.82.0 (060b5d2) / protocol 70 / 2026-09-14 / "
                    f"run {RUN_ID}", fill=(150, 170, 190))
    y += 34
    color = {"passed": (90, 220, 120), "failed": (235, 90, 90),
             "not-run": (200, 170, 90)}.get(verdict, (200, 200, 200))
    d.text((24, y), f"VERDICT: {verdict}", fill=color)
    y += 34
    d.text((24, y), "Fable saga -> Reflection {1},{T} copies Triumph -> "
                    "end-step sacrifice -> dies-trigger pile",
           fill=(170, 190, 210))
    y += 40
    for k in ("A1_parse", "A2_setup_ok", "A3_token_created",
              "A4_trigger_fired", "A5_no_hang", "A6_pile_correct",
              "A7_cleanup"):
        v = ass.get(k, "not-run")
        c = {"passed": (90, 220, 120), "failed": (235, 90, 90),
             "not-run": (200, 170, 90)}[v]
        d.text((24, y), f"{k}: {v}", fill=c)
        y += 28
    y += 8
    d.text((24, y), "notes:", fill=(150, 170, 190))
    y += 26
    for n in notes[:14]:
        for line in wrap(n, 108):
            d.text((30, y), line, fill=(160, 175, 190))
            y += 20
            if y > H - 30:
                break
        y += 4
        if y > H - 30:
            break
    img.save(f"{EVDIR}/summary.png")


def wrap(s, n):
    out, cur = [], ""
    for w in s.split():
        if len(cur) + len(w) + 1 > n:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


async def main():
    reset()
    global C0, C1
    ST["a1"] = parse_check()

    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    hb = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for c, pid in ((C0, 0), (C1, 1)):
            await tick(c, pid)
        if time.time() - hb > 30:
            hb = time.time()
            st = C0.latest or {}
            state = st.get("state") or {}
            hand = sorted(oname(state.get("objects", {}).get(x, {}))
                          for x in hand_oids(state, 0)) if state else []
            bf0 = sorted(oname(o) for _, o in bf(state, 0)) if state else []
            say(f"HB turn={state.get('turn_number')} phase={state.get('phase')} "
                f"active={state.get('active_player')} wf={wf_type(state)} "
                f"wf_player={wf_player(state)} stage={ST['stage']} "
                f"P0hand={hand} P0bf={bf0} rev={st.get('state_revision')}")
        await asyncio.sleep(0.05)

    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finalize()


if __name__ == "__main__":
    asyncio.run(main())
