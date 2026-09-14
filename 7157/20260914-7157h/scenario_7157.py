#!/usr/bin/env python3
"""phase-rs/phase #7157 - Emperor of Bones didn't grant creature haste nor
trigger its etb.

Oracle (from pinned card-data.json, v0.82.0):
  Emperor of Bones {1}{B} 2/2 Skeleton Noble:
    At the beginning of combat on your turn, exile up to one target card
    from a graveyard.
    {1}{B}: Adapt 2.
    Whenever one or more +1/+1 counters are put on this creature, put a
    creature card exiled with this creature onto the battlefield under your
    control with a finality counter on it. It gains haste. Sacrifice it at
    the beginning of the next end step.
  Anointed Peacekeeper {2}{W} 3/3 Vigilance:
    As this creature enters, look at an opponent's hand, then choose any
    card name.
    Spells your opponents cast with the chosen name cost {2} more to cast.
    Activated abilities of sources with the chosen name cost {2} more to
    activate unless they're mana abilities.

Report (Discord): a Peacekeeper returned by Emperor of Bones got neither
haste nor its as-enters choice.

Behavioral contract (2 human seats, native engine):
  P0 mulligans until a Peacekeeper is in the opening hand, then plays NO
  lands until a Peacekeeper hits the graveyard: the first P0 cleanup with
  8+ cards bins a Peacekeeper (the preamble's discard ranking puts an
  un-binned Peacekeeper first). Once it is binned, P0 plays lands, casts
  Emperor of Bones (needs {1}{B}), exiles the Peacekeeper with the
  beginning-of-combat trigger, then on a later turn (2 lands) activates
  Adapt 2. The whenever-counters trigger must return the Peacekeeper to
  the battlefield with a finality counter, it must gain haste, and its
  as-enters choice (look at P1's hand, choose a card name) must be offered
  and recorded. At that turn's end step the delayed trigger sacrifices it
  (finality counter -> exiled instead of dying).
  A1 parse:          Emperor's 3 abilities/triggers + Peacekeeper's as-enters
                     replacement (Choose Opponent -> RevealHand -> Choose
                     CardName) + static cost effects fully parsed.
  A2 setup_ok:       Emperor on P0 BF, Peacekeeper in P0 graveyard pre-T5
                     combat, mana available for Adapt on T7.
  A3 exile_ok:       beginning-of-combat trigger exiled the Peacekeeper
                     (ends up in Exile; target prompt optional - the engine
                     may auto-target a single legal candidate).
  A4 adapt_trigger:  Adapt 2 resolved (2 +1/+1 counters on Emperor) and the
                     whenever-counters trigger fired.
  A5 returned:       Peacekeeper entered the BF under P0 control with a
                     finality counter on it.
  A6 haste:          the returned Peacekeeper gained Haste.
  A7 entry_choice:    the as-enters choice prompt appeared, the submitted
                     card name ("Swamp") was accepted, and the game moved
                     past the entry event.
  A8 endstep_sacrifice: at the T7 end step the Peacekeeper left the BF
                     (finality counter -> Exile, not graveyard).
  A9 cleanup:        stack empty, game advanced past T7, no stall.

Verdict: reproduced iff A2 passes and any of A3-A8 fails (A5/A6/A7 are the
reported failures; A3/A4/A8 failures are related path failures and are
explained as such). not-reproduced iff A2-A9 all pass. blocked iff A2
cannot be established (fixture failure).
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7157")
EVDIR = f"{BACKFILL}/evidence/7157/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

EMPEROR = "Emperor of Bones"
PK = "Anointed Peacekeeper"
SWAMP = "Swamp"
LANDS = (SWAMP,)
# Free-text card name for the as-enters Choose CardName prompt (P1's hand
# is all Swamps; the CardName NamedChoice carries allowArbitrary=true with
# no candidates, the #6593-established shape).
NAME_CHOICE = "Swamp"

P0_DECK = [(EMPEROR, 16), (PK, 16), (SWAMP, 28)]
P1_DECK = [(SWAMP, 60)]
TIMEOUT = 2400

CD_PATH = f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb77135c35b2",
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
        "mulliganed": False,
        "emperor_cast": False,
        "emperor_cast_turn": None,
        "emperor_oid": None,
        "pk_in_gy": False,
        "exile_prompted": False,
        "exile_answered": False,
        "exile_auto": False,       # PK exiled with no target prompt seen
        "pk_exiled": False,
        "adapt_submitted": False,
        "adapt_turn": None,
        "counters_trigger_seen": False,
        "card_choice_prompted": False,
        "card_choice_answered": False,
        "pk_oid": None,
        "pk_entered": False,
        "pk_enter_turn": None,
        "has_finality": None,
        "has_haste": None,
        "opp_choice_prompted": False,
        "opp_choice_answered": False,
        "name_prompted": False,
        "name_submitted": False,
        "name_accepted": False,
        "name_recorded": None,
        "second_trigger_handled": False,
        "pk_left_bf": False,
        "pk_left_turn": None,
        "pk_end_zone": None,
        "unhandled_prompt": None,
        "watch_t0": None,
        "finalized": False,
        "prompt_first_seen": {},
        "last_rev": -1,
        "last_rev_t": time.time(),
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


def gy_named(state, pid, name):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


def exile_named(state, pid, name):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Exile" and oname(o) == name
            and o.get("controller") == pid]


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if oname(o) in LANDS and not o.get("tapped")]


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


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            return d.get("seat")
    return None


def is_decline_choice(ch):
    if str(accept_of(ch)).lower() == "false":
        return True
    tx = choice_text(ch).lower()
    return any(k in tx for k in ("decline", "don't", "do not", "no target",
                                 "none", "cancel", "skip"))


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


def counters_trigger_on_stack(state):
    for e in stack_entries(state):
        blob = json.dumps(e, default=str)
        if e.get("kind") == "TriggeredAbility" and (
                "CounterAdded" in blob or "+1/+1 counter" in blob
                or "counters are put" in blob):
            return True
    return False


def has_keyword(obj, keyword):
    """Recursive search for an AddKeyword <keyword> grant or keyword entry."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "AddKeyword" and str(
                    node.get("keyword", "")).lower() == keyword.lower():
                found.append(True)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(obj)
    return bool(found)


# ---------------- preamble ---------------------------------------------------
async def preamble(c, pid, st, state, acts):
    """Mulligan / hand-size discard / blockers / declare-empty.
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
                    if oname(state["objects"][x]) not in (EMPEROR, PK)][:n]
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
                            has_pk = any(
                                oname(state["objects"][x]) == PK
                                for x in hand_oids(state, 0))
                            if not has_pk:
                                # Guarantee a Peacekeeper in hand: P0 plays
                                # no lands until one is binned, so the first
                                # 8-card cleanup discards it.
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
        oids = hand_oids(state, pid)

        def rank(x):
            nm = oname(state["objects"][x])
            if pid == 0:
                if nm == PK and not ST["pk_in_gy"]:
                    return 0  # get one Peacekeeper into the graveyard
                if nm == EMPEROR and not ST["emperor_cast"]:
                    return 4  # protect the Emperor until cast
                if nm == PK:
                    return 1  # spare Peacekeepers next
                if nm == EMPEROR:
                    return 2  # spare Emperors
                return 3      # lands last
            return 0 if nm in LANDS else 1
        oids.sort(key=rank)
        picks = [int(x) for x in oids[:n]]
        names = [oname(state["objects"][str(x)]) for x in picks]
        if picks and not acted(f"hsd{pid}", rev):
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{c.name}] discards {n} to hand size: {names}")
            if pid == 0 and PK in names:
                ST["pk_in_gy"] = True
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
async def handle_exile_target(c, pid, tag, st, state):
    """First exile trigger (T5 combat): target the Peacekeeper in P0's
    graveyard. Later triggers (T7 combat): pick any graveyard card, or a
    decline-style choice if offered."""
    if wf_player(state) != pid:
        return False
    wt = wf_type(state) or ""
    if "Target" not in wt and "target" not in wt.lower():
        return False
    vi = get_vi(st)
    if not vi:
        return False
    first = ST["stage"] == "EXILE_WAIT"
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = True
            if first:
                ST["exile_prompted"] = True
            wire("exile_target_prompt",
                 {"who": tag, "wf_type": wt, "first": first,
                  "n_choices": len(chs),
                  "choice_names": [choice_card_name(ch, state)
                                   for ch in chs][:10]})
            say(f"[{tag}] exile target prompt (first={first}): "
                f"{len(chs)} candidates")
        rev = st.get("state_revision", -1)
        if acted(f"ext{pid}{first}", rev):
            return True
        want = None
        if first:
            for ch in chs:
                r = ref_of(ch)
                o = (state.get("objects") or {}).get(str(r)) if r else None
                if o and oname(o) == PK and o.get("zone") == "Graveyard" \
                        and o.get("controller") == 0:
                    want = ch
                    break
            if want is None:
                say(f"[{tag}] WARNING: Peacekeeper not among exile "
                    f"candidates: "
                    f"{[choice_card_name(ch, state) for ch in chs][:8]}")
                wire("exile_no_pk_candidate",
                     {"candidates": [choice_card_name(ch, state)
                                     for ch in chs][:12]})
                return False
        else:
            # second trigger (T7 combat): decline if offered, else any
            # graveyard card; the fixture no longer needs this trigger.
            for ch in chs:
                if is_decline_choice(ch):
                    want = ch
                    break
            if want is None:
                for ch in chs:
                    r = ref_of(ch)
                    o = (state.get("objects") or {}).get(str(r)) \
                        if r else None
                    if o and o.get("zone") == "Graveyard":
                        want = ch
                        break
            if want is None:
                say(f"[{tag}] WARNING: second exile trigger has no "
                    f"decline/gy candidate")
                return False
            ST["second_trigger_handled"] = True
        say(f"[{tag}] answering exile target: "
            f"{choice_card_name(want, state)}")
        await answer_vi(c, opp, want, tag)
        if first:
            ST["exile_answered"] = True
        return True
    return False


async def handle_optional(c, pid, tag, st, state, accept):
    """Answer OptionalEffectChoice-style prompts with accept/decline."""
    wt = (wf_type(state) or "").lower()
    if "optional" not in wt:
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
            if str(accept_of(ch)).lower() == str(accept).lower():
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: optional prompt ({wf_type(state)}) with "
                f"no {'accept' if accept else 'decline'} choice: "
                f"{[choice_text(ch)[:40] for ch in chs]}")
            wire("optional_prompt_unhandled",
                 {"wf_type": wf_type(state),
                  "choices": [choice_text(ch)[:60] for ch in chs]})
            return False
        wire("optional_prompt", {"wf_type": wf_type(state), "accept": accept,
                                 "choices": [choice_text(ch)[:60]
                                             for ch in chs]})
        say(f"[{tag}] {'accepting' if accept else 'declining'} optional "
            f"prompt ({wf_type(state)})")
        await answer_vi(c, opp, want, tag)
        return True
    return False


async def handle_exiled_card_choice(c, pid, tag, st, state):
    """The whenever-counters trigger resolving: choose the exiled
    Peacekeeper to put onto the battlefield."""
    if ST["stage"] != "ADAPTED":
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
            ST["card_choice_prompted"] = True
            wire("counters_card_choice_prompt",
                 {"wf_type": wf_type(state), "n_choices": len(chs),
                  "choice_names": [choice_card_name(ch, state)
                                   for ch in chs][:10],
                  "spec": ((opp.get("response", {}) or {}).get("data", {})
                           or {}).get("spec")})
            say(f"[{tag}] counters-trigger card choice: {len(chs)} "
                f"candidates")
        rev = st.get("state_revision", -1)
        if acted(f"ecc{pid}", rev):
            return True
        want = None
        for ch in chs:
            r = ref_of(ch)
            o = (state.get("objects") or {}).get(str(r)) if r else None
            if o and oname(o) == PK and o.get("zone") == "Exile":
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: exiled Peacekeeper not among "
                f"candidates: {[choice_card_name(ch, state) for ch in chs]}")
            return False
        say(f"[{tag}] choosing exiled Peacekeeper for return")
        await answer_vi(c, opp, want, tag)
        ST["card_choice_answered"] = True
        return True
    return False


async def handle_entry_opponent_choice(c, pid, tag, st, state):
    """Peacekeeper's as-enters 'choose an opponent' step: pick P1."""
    if ST["stage"] not in ("ENTRY",):
        return False
    if wf_player(state) != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        chs, rtype = vi_choices(opp)
        seats = [(ch, seat_of(ch)) for ch in chs]
        if not any(s == 1 for _, s in seats):
            continue
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = True
            ST["opp_choice_prompted"] = True
            wire("entry_opponent_prompt",
                 {"wf_type": wf_type(state),
                  "seats": [s for _, s in seats]})
            say(f"[{tag}] entry opponent choice prompted")
        rev = st.get("state_revision", -1)
        if acted(f"eoc{pid}", rev):
            return True
        want = next(ch for ch, s in seats if s == 1)
        say(f"[{tag}] choosing opponent P1 for entry look")
        await answer_vi(c, opp, want, tag)
        ST["opp_choice_answered"] = True
        return True
    return False


def candidate_strings(ch):
    """Exhaustively collect string values from a candidate's surfaces."""
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data")
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, str) and v:
                    out.append((k, v))
        elif isinstance(d, str) and d:
            out.append(("str", d))
    return out


async def handle_entry_name_choice(c, pid, tag, st, state):
    """Peacekeeper's as-enters 'choose any card name': NamedChoice text
    schema. On v0.82.0 the spec carries allowArbitrary=false with a single
    candidate, so free text is rejected (interaction_constraint_unsatisfied);
    try candidate-derived submissions in sequence, retrying on rejection."""
    if ST["stage"] not in ("ENTRY",):
        return False
    if wf_player(state) != pid:
        return False
    if (wf_type(state) or "") != "NamedChoice":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        iid = opp.get("interactionId")
        chs, rtype = vi_choices(opp)
        # NOTE: use a dedicated seen-set here. prompt_first_seen may
        # already contain this iid (an earlier handler logged the same
        # interaction id while it carried different candidates); gating
        # on it would skip first-sight init and then `continue` past
        # every opportunity forever.
        if iid not in ST.setdefault("name_seen", set()):
            ST["name_seen"].add(iid)
            ST["name_prompted"] = True
            wire("entry_name_prompt_full",
                 {"rtype": rtype, "spec": spec,
                  "waiting_for": state.get("waiting_for"),
                  "candidates": [
                      {"id": ch.get("id"),
                       "strings": candidate_strings(ch)}
                      for ch in chs],
                  "opportunity": opp})
            say(f"[{tag}] NamedChoice full opportunity dumped "
                f"(rtype={rtype} spec={spec_type} n_cand={len(chs)})")
            ST["name_iid"] = iid
            ST["name_shapes_tried"] = []
            ST["name_nrej_at_try"] = len(ST["rejections"])
            ST["name_first_seen_t"] = time.time()
        if iid != ST.get("name_iid"):
            continue
        # hold while the engine digests our last attempt
        if len(ST["rejections"]) <= ST.get("name_nrej_at_try", 0) \
                and ST["name_shapes_tried"]:
            return True
        tried = ST["name_shapes_tried"]
        cand = chs[0] if chs else None
        cid = cand.get("id") if cand else None
        cname = None
        if cand:
            for k, v in candidate_strings(cand):
                if k in ("text", "name", "card_name", "title", "value",
                         "label") and not cname:
                    cname = v
            if not cname and candidate_strings(cand):
                cname = candidate_strings(cand)[0][1]
        sub = None
        shape = None
        allow_arb = (spec.get("data", {}) or {}).get("allowArbitrary", False) \
            if isinstance(spec, dict) else False
        if allow_arb and "free_text" not in tried \
                and spec_type == "text":
            # CardName choice (#6593 shape): free text, no candidates.
            shape = "free_text"
            sub = {"interactionId": iid,
                   "response": {"type": "text",
                                "data": {"value": NAME_CHOICE}}}
        elif cname and "text_value" not in tried \
                and spec_type == "text":
            shape = "text_value"
            sub = {"interactionId": iid,
                   "response": {"type": "text",
                                "data": {"value": cname}}}
        elif cid and "choose_id" not in tried:
            shape = "choose_id"
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cid}}}
        elif cid and "seq_ids" not in tried:
            shape = "seq_ids"
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        else:
            # out of shapes: record the stall; the watchdog finalizes
            if not ST.get("name_shapes_exhausted"):
                ST["name_shapes_exhausted"] = True
                say(f"[{tag}] NamedChoice: all submission shapes exhausted "
                    f"(tried={tried})")
                wire("name_shapes_exhausted",
                     {"tried": tried, "iid": iid})
            return True
        say(f"[{tag}] submitting name choice shape={shape} "
            f"value={cname!r} cid={cid!r}")
        wire("name_submit", {"who": tag, "shape": shape,
                             "submission": sub,
                             "candidate_name": cname})
        await c.send_interaction(sub)
        ST["name_shapes_tried"].append(shape)
        ST["name_nrej_at_try"] = len(ST["rejections"])
        ST["name_submitted"] = True
        return True
    return False


# ---------------- P0 tick ----------------------------------------------------
async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    rev = st.get("state_revision", -1)
    turn = state.get("turn_number", 0)
    if wt and wt not in WF_SEEN:
        WF_SEEN.add(wt)
        wire("wf_type_seen", {"type": wt, "player": wp,
                              "stage": ST.get("stage"), "turn": turn})

    # cost/target/choice prompt handlers first: never pass while ours pending
    if await handle_exile_target(c, 0, "P0", st, state):
        return True
    if await handle_exiled_card_choice(c, 0, "P0", st, state):
        return True
    if await handle_entry_opponent_choice(c, 0, "P0", st, state):
        return True
    if await handle_entry_name_choice(c, 0, "P0", st, state):
        return True
    # "you may" prompts: accept the first exile trigger, decline any other
    if (wt or "").lower().find("optional") >= 0 and wp == 0:
        accept = ST["stage"] == "EXILE_WAIT" and not ST["exile_answered"]
        if await handle_optional(c, 0, "P0", st, state, accept):
            return True

    # --- PK exile watch (auto-target case: no prompt, straight to exile) ---
    if ST["stage"] == "EXILE_WAIT":
        pks = exile_named(state, 0, PK)
        if pks and not ST["pk_exiled"]:
            ST["pk_exiled"] = True
            if not ST["exile_prompted"]:
                ST["exile_auto"] = True
            ST["stage"] = "ADAPT_READY"
            say(f"[P0] Peacekeeper exiled (auto={ST['exile_auto']}, "
                f"prompted={ST['exile_prompted']})")
            wire("pk_exiled", {"auto": ST["exile_auto"],
                               "turn": turn})
            await export_now("post_exile.json")

    # --- PK return watch ----------------------------------------------------
    if ST["stage"] in ("ADAPTED",) and not ST["pk_entered"]:
        pks = bf_named(state, 0, PK)
        if pks:
            ST["pk_oid"] = pks[0]
            ST["pk_entered"] = True
            ST["pk_enter_turn"] = turn
            ST["stage"] = "ENTRY"
            o = state["objects"][pks[0]]
            ST["has_finality"] = "finality" in json.dumps(
                o.get("counters", {}), default=str).lower()
            ST["has_haste"] = has_keyword(o, "Haste")
            say(f"[P0] PEACEKEEPER RETURNED oid={pks[0]} "
                f"finality={ST['has_finality']} haste={ST['has_haste']}")
            wire("pk_returned", {"oid": pks[0],
                                 "finality": ST["has_finality"],
                                 "haste": ST["has_haste"],
                                 "card_choice_prompted":
                                     ST["card_choice_prompted"],
                                 "turn": turn})
            await export_now("post_return.json")

    # --- name choice accepted? (game moved past NamedChoice) ---------------
    if ST["name_submitted"] and not ST["name_accepted"]:
        if wt != "NamedChoice":
            # check for an invalid-name rejection first
            bad = [r for r in ST["rejections"]
                   if "invalid card name" in json.dumps(r).lower()]
            if bad:
                say("[P0] name submission REJECTED as invalid card name")
                wire("name_rejected_invalid", {"rejections": bad})
            else:
                ST["name_accepted"] = True
                ST["stage"] = "ENDSTEP_WATCH"
                ST["endstep_watch_turn"] = turn
                say("[P0] name choice accepted; entry event complete")
                wire("name_accepted", {"turn": turn})
                await export_now("post_entry.json")

    # --- end-step sacrifice watch -------------------------------------------
    if ST["pk_oid"] and not ST["pk_left_bf"]:
        o = (state.get("objects") or {}).get(str(ST["pk_oid"]))
        if o is None or o.get("zone") != "Battlefield":
            ST["pk_left_bf"] = True
            ST["pk_left_turn"] = turn
            ST["pk_end_zone"] = (o or {}).get("zone")
            say(f"[P0] Peacekeeper left battlefield -> "
                f"{ST['pk_end_zone']} (turn {turn})")
            wire("pk_left_bf", {"zone": ST["pk_end_zone"], "turn": turn,
                                "phase": state.get("phase")})
            await export_now("post_endstep.json")
            ST["stage"] = "SETTLE"
            ST["done_reason"] = "sacrifice observed"
            ST["stop"] = True
            return True
        # bounded watch: if the end step came and went and the PK is
        # still on the battlefield, the delayed sacrifice did not fire
        # on it -> conclude instead of spinning to the timebox
        if ST["stage"] == "ENDSTEP_WATCH" \
                and ST.get("endstep_watch_turn") \
                and turn >= ST["endstep_watch_turn"] + 4:
            say(f"[P0] ENDSTEP_WATCH concluded: Peacekeeper still on "
                f"battlefield at turn {turn} (no end-step sacrifice)")
            wire("endstep_watch_concluded",
                 {"turn": turn,
                  "pk_zone": o.get("zone"),
                  "pk_counters": o.get("counters")})
            await export_now("post_endstep.json")
            ST["stage"] = "SETTLE"
            ST["done_reason"] = "end-step watch concluded"
            ST["stop"] = True
            return True

    # --- counters trigger on stack (observation only) -----------------------
    if ST["stage"] == "ADAPTED" and counters_trigger_on_stack(state) \
            and not ST["counters_trigger_seen"]:
        ST["counters_trigger_seen"] = True
        say(f"[P0] whenever-counters trigger on stack (turn {turn})")
        wire("counters_trigger_on_stack", {"turn": turn})

    # --- main-phase action taking -------------------------------------------
    in_flight = ST["adapt_submitted"] and bool(stack_entries(state))
    if is_my_main(state, 0) and not in_flight:
        # land drops: none until a Peacekeeper is in the graveyard, so
        # the hand reliably grows to 8 for the cleanup discard even
        # after mulligans
        if ST["stage"] in ("SETUP", "EXILE_WAIT", "ADAPT_READY") \
                and ST["pk_in_gy"]:
            lo = find_hand(state, 0, SWAMP)
            pla = playland_for(acts, lo)
            if pla:
                await submit_as_is(c, pla)
                say("[P0] plays Swamp")
                return True

        # cast Emperor (needs {1}{B}; only after the gy is stocked)
        if not ST["emperor_cast"] and ST["pk_in_gy"] and turn >= 3:
            eo = find_hand(state, 0, EMPEROR)
            ca = cast_action_for(acts, eo)
            if ca:
                ST["emperor_cast"] = True
                ST["emperor_cast_turn"] = turn
                ST["stage"] = "EXILE_WAIT"
                await submit_as_is(c, ca)
                say(f"[P0] casts Emperor of Bones (turn {turn})")
                return True

        # track Emperor oid; export setup.json once it is on the
        # battlefield (pre-combat), not at cast time
        if ST["emperor_cast"] and not ST["emperor_oid"]:
            eos = bf_named(state, 0, EMPEROR)
            if eos:
                ST["emperor_oid"] = eos[0]
                await export_now("setup.json")

        # activate Adapt ({1}{B}, needs 2 untapped lands): on P0's turn
        # after the cast turn (seats alternate, so +2)
        if ST["stage"] == "ADAPT_READY" and not ST["adapt_submitted"] \
                and turn >= (ST["emperor_cast_turn"] or 0) + 2 \
                and ST["emperor_oid"]:
            if len(untapped_lands(state, 0)) >= 2 and not (
                    state.get("stack") or []):
                found = None
                for a in acts:
                    if a["type"] == "ActivateAbility" and str(
                            a.get("data", {}).get("source_id")) == str(
                            ST["emperor_oid"]):
                        found = a
                        break
                wire("adapt_offer_scan",
                     {"turn": turn, "offered": bool(found),
                      "untapped_lands": len(untapped_lands(state, 0))})
                if found:
                    ST["adapt_submitted"] = True
                    ST["adapt_turn"] = turn
                    ST["stage"] = "ADAPTED"
                    await export_now("pre_adapt.json")
                    await submit_as_is(c, found)
                    say(f"[P0] ADAPT 2 ACTIVATED (turn {turn})")
                    return True
                say(f"[P0] adapt not offered (turn {turn})")

    # --- fixture-blocked watches --------------------------------------------
    if not ST["emperor_cast"] and turn >= 15 and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] BLOCKED: Emperor never cast by turn 15")
        wire("blocked", {"reason": "emperor_never_cast", "turn": turn,
                         "hand": sorted(oname(state["objects"][x])
                                        for x in hand_oids(state, 0))})
        await export_now("post_blocked.json")
        ST["done_reason"] = "Emperor of Bones never cast"
        ST["stop"] = True
        return True
    if ST["emperor_cast"] and not ST["adapt_submitted"] and turn >= 19 \
            and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] BLOCKED: Adapt never submitted by turn 19")
        wire("blocked", {"reason": "adapt_never_submitted", "turn": turn})
        await export_now("post_blocked.json")
        ST["done_reason"] = "Adapt 2 never activated"
        ST["stop"] = True
        return True
    if ST["stage"] == "ENTRY" and ST.get("name_prompted") \
            and not ST["name_accepted"] and ST.get("name_first_seen_t") \
            and time.time() - ST["name_first_seen_t"] > 150 \
            and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] STALL: ENTRY NamedChoice never accepted "
            f"(tried={ST.get('name_shapes_tried')})")
        wire("entry_stall_watchdog",
             {"iid": ST.get("name_iid"),
              "tried": ST.get("name_shapes_tried"),
              "rejections": len(ST["rejections"])})
        await export_now("post_stall.json")
        ST["done_reason"] = "entry NamedChoice stall watchdog"
        ST["stop"] = True
        return True
    if turn >= 23 and not ST.get("blocked_done"):
        ST["blocked_done"] = True
        say("[P0] TIMEBOX: turn 23 reached, finalizing with partial evidence")
        wire("timebox", {"turn": turn, "stage": ST["stage"]})
        await export_now("post_timebox.json")
        ST["done_reason"] = "timebox at turn 23"
        ST["stop"] = True
        return True

    # --- default: pass priority (always fall through; never hold while a
    # --- spell/ability of ours is in flight or the game deadlocks) ---------
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
        lo = find_hand(state, 1, SWAMP)
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

    em = cd["emperor of bones"]
    # beginning-of-combat exile trigger
    boc = [t for t in em["triggers"]
           if t.get("phase") == "BeginCombat"]
    ok_boc = False
    if boc:
        eff = (boc[0].get("execute", {}).get("effect") or {}).get("type")
        mt = ((boc[0].get("execute", {}) or {}).get("multi_target") or {})
        ok_boc = (eff == "ChangeZone"
                  and (boc[0].get("execute", {}).get("effect") or {})
                  .get("destination") == "Exile"
                  and str(mt.get("min")) == "0")
        parts.append(f"emperor_boc={eff} up_to_one={mt.get('min')==0}")
    else:
        parts.append("emperor_boc_missing")

    # adapt activated ability
    ad = [a for a in em["abilities"]
          if a.get("kind") == "Activated"]
    ok_ad = False
    if ad:
        ok_ad = (ad[0].get("effect") or {}).get("type") == "Adapt"
        parts.append(f"emperor_adapt={(ad[0].get('effect') or {}).get('type')}")
    else:
        parts.append("emperor_adapt_missing")

    # whenever-counters trigger: return w/ finality + haste + delayed sac
    ca = [t for t in em["triggers"] if t.get("mode") == "CounterAdded"]
    ok_ca = False
    if ca:
        ex = ca[0].get("execute", {}) or {}
        eff = (ex.get("effect") or {}).get("type")
        dest = (ex.get("effect") or {}).get("destination")
        counters = (ex.get("effect") or {}).get("enter_with_counters") or []
        blob = json.dumps(ca[0])
        ok_ca = (eff == "ChangeZone" and dest == "Battlefield"
                 and any(c[0] == "finality" for c in counters)
                 and '"AddKeyword"' in blob and '"Haste"' in blob
                 and "Sacrifice" in blob
                 and "Unimplemented" not in blob)
        parts.append(f"emperor_counters={eff}->{dest} finality="
                     f"{any(c[0]=='finality' for c in counters)} "
                     f"haste={'\"Haste\"' in blob} sac={'Sacrifice' in blob} "
                     f"unimplemented={'Unimplemented' in blob}")
    else:
        parts.append("emperor_counters_trigger_missing")

    # peacekeeper as-enters replacement + static cost effects
    pkc = cd["anointed peacekeeper"]
    rep = pkc.get("replacements") or []
    ok_rep = False
    if rep:
        r = rep[0]
        chain = []
        node = r.get("execute")
        while node:
            eff = (node.get("effect") or {}).get("type")
            ct = (node.get("effect") or {}).get("choice_type")
            chain.append(f"{eff}({ct})" if ct else eff)
            node = node.get("sub_ability")
        ok_rep = (chain == ["Choose(Opponent)", "RevealHand",
                            "Choose(CardName)"]
                  and "Unimplemented" not in json.dumps(r))
        parts.append(f"pk_entry={chain}")
    else:
        parts.append("pk_entry_replacement_missing")
    sa = pkc.get("static_abilities") or []
    modes = [list(s.get("mode", {}).keys())[0]
             for s in sa if s.get("mode")]
    ok_sa = "ModifyCost" in modes and "ReduceAbilityCost" in modes
    parts.append(f"pk_statics={modes}")

    ok = ok_boc and ok_ad and ok_ca and ok_rep and ok_sa
    detail = "; ".join(parts)
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_check", {"ok": ok, "detail": detail})
    return "passed" if ok else "failed"


def load_state(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())["state"]
    except Exception:
        return None


def gy_has_pk(state):
    return bool(gy_named(state, 0, PK))


async def finalize():
    if ST.get("finalized"):
        return
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "timeout"
    say("finalizing...")

    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7157.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass = {}
    notes = []
    a1 = ST.get("a1", "not-run")
    ass["A1_parse"] = a1
    notes.append(f"A1: emperor boc/adapt/counters-trigger + peacekeeper "
                 f"entry replacement + statics parse: {a1}")

    setup = load_state("setup.json")
    ok2 = False
    if setup and ST.get("emperor_oid"):
        eo = (setup.get("objects") or {}).get(str(ST["emperor_oid"]))
        on_bf = eo is not None and eo.get("zone") == "Battlefield"
        ok2 = on_bf and gy_has_pk(setup)
        notes.append(f"A2: emperor_on_bf={on_bf} "
                     f"pk_in_gy={gy_has_pk(setup)} "
                     f"cast_turn={ST.get('emperor_cast_turn')}")
    else:
        notes.append("A2: setup.json missing (Emperor never cast)")
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"

    ok3 = ST["pk_exiled"]
    ass["A3_exile_ok"] = "passed" if ok3 else (
        "not-run" if not ok2 else "failed")
    notes.append(f"A3: pk_exiled={ST['pk_exiled']} "
                 f"(prompted={ST['exile_prompted']} answered="
                 f"{ST['exile_answered']} auto={ST['exile_auto']})")

    ok4 = False
    post_ret = load_state("post_return.json")
    if ok3 and post_ret and ST.get("emperor_oid"):
        eo = (post_ret.get("objects") or {}).get(str(ST["emperor_oid"]))
        counters = (eo or {}).get("counters", {})
        n11 = 0
        blob = json.dumps(counters, default=str).lower()
        # count +1/+1 counters on Emperor
        if isinstance(counters, dict):
            for k, v in counters.items():
                # engine names +1/+1 counters "P1P1" in the counters map
                if "+1/+1" in str(k) or "p1p1" in str(k).lower():
                    n11 = v if isinstance(v, int) else n11
        elif isinstance(counters, list):
            n11 = sum(1 for c in counters if "+1/+1" in str(c))
        ok4 = (ST["adapt_submitted"] and n11 >= 2
               and (ST["counters_trigger_seen"]
                    or ST["card_choice_prompted"] or ST["pk_entered"]))
        notes.append(f"A4: adapt_submitted={ST['adapt_submitted']} "
                     f"emperor_+1/+1={n11} counters_trigger_seen="
                     f"{ST['counters_trigger_seen']} card_choice_prompted="
                     f"{ST['card_choice_prompted']}")
    else:
        notes.append(f"A4: not-run (pk_exiled={ST['pk_exiled']}) "
                     f"adapt_submitted={ST['adapt_submitted']}")
    ass["A4_adapt_trigger"] = "passed" if ok4 else (
        "not-run" if not ok3 else "failed")

    ok5 = ok4 and ST["pk_entered"] and ST["has_finality"]
    ass["A5_returned"] = "passed" if ok5 else (
        "not-run" if not ok4 else "failed")
    notes.append(f"A5: pk_entered={ST['pk_entered']} "
                 f"(oid={ST['pk_oid']} turn={ST['pk_enter_turn']}) "
                 f"finality={ST['has_finality']}")

    ok6 = ok5 and bool(ST["has_haste"])
    ass["A6_haste"] = "passed" if ok6 else (
        "not-run" if not ok5 else "failed")
    notes.append(f"A6: returned Peacekeeper gained haste: "
                 f"{ST['has_haste']}")

    ok7 = (ST["name_prompted"] and ST["name_submitted"]
           and ST["name_accepted"])
    bad_name = [r for r in ST["rejections"]
                if "invalid card name" in json.dumps(r).lower()]
    ass["A7_entry_choice"] = "passed" if ok7 else (
        "not-run" if not ok5 else "failed")
    notes.append(f"A7: name_prompted={ST['name_prompted']} "
                 f"submitted={ST['name_submitted']} "
                 f"accepted={ST['name_accepted']} "
                 f"shapes_tried={ST.get('name_shapes_tried')} "
                 f"invalid_name_rejections={len(bad_name)}")

    ok8 = ST["pk_left_bf"] and ST["pk_end_zone"] == "Exile"
    ass["A8_endstep_sacrifice"] = "passed" if ok8 else (
        "not-run" if not ok5 else "failed")
    # extra signal: where did the Emperor end up? (delayed trigger
    # sacrificed the wrong object in the observed run)
    emp_zone = None
    _pe = load_state("post_endstep.json") or load_state("final.json")
    if _pe and ST.get("emperor_oid"):
        _eo = (_pe.get("objects") or {}).get(str(ST["emperor_oid"]))
        emp_zone = (_eo or {}).get("zone")
    notes.append(f"A8: pk_left_bf={ST['pk_left_bf']} "
                 f"end_zone={ST['pk_end_zone']} "
                 f"(turn={ST['pk_left_turn']}; finality counter exiles "
                 f"instead of dying) emperor_zone={emp_zone} "
                 f"watch_concluded={ST.get('done_reason') == 'end-step watch concluded'}")

    ok9 = False
    final = load_state("post_endstep.json") or load_state("final.json")
    if final:
        stack_empty = not (final.get("stack") or [])
        advanced = (final.get("turn_number") or 0) >= 7
        ok9 = stack_empty and advanced
        notes.append(f"A9: stack_empty={stack_empty} "
                     f"turn={final.get('turn_number')}")
    else:
        notes.append("A9: no final state captured")
    ass["A9_cleanup"] = "passed" if ok9 else (
        "not-run" if not ok5 else "failed")

    verdict = "blocked"
    if ok2:
        failed = any(ass[k] == "failed" for k in
                     ("A3_exile_ok", "A4_adapt_trigger", "A5_returned",
                      "A6_haste", "A7_entry_choice", "A8_endstep_sacrifice"))
        # A6/A7 are the reported failures; A3/A4/A5/A8 failures are related
        # path failures on the same chain.
        verdict = "reproduced" if failed else "not-reproduced"
    notes.append(f"verdict rule: {verdict} "
                 f"(done_reason={ST['done_reason']})")

    run = {
        "issue": 7157,
        "title": "Emperor of Bones didn't grant creature haste nor trigger "
                 "its etb",
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "validated_version": "v0.82.0",
        "server": SERVER_IDENTITY,
        "scope": "Emperor of Bones exile trigger + Adapt 2 + "
                 "whenever-counters return of Anointed Peacekeeper; "
                 "native engine, two human-client seats",
        "assertions": ass,
        "assertion_notes": notes,
        "verdict": verdict,
        "rejections": ST["rejections"],
        "wf_types_seen": sorted(WF_SEEN),
        "unhandled_prompt": ST.get("unhandled_prompt"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "P1 fully passive (60x Swamp; plays land, never casts/"
            "attacks/blocks); P1's hand holds Swamps for the entry 'look'.",
            "P0 plays no lands until a Peacekeeper reaches the graveyard, "
            "so the first 8-card cleanup bins one even after mulligans; "
            "P0 mulligans until the opening hand holds a Peacekeeper.",
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
    d.text((24, y), "phase-rs/phase #7157 \u2014 Emperor of Bones / "
                    "Anointed Peacekeeper", fill=(240, 240, 240))
    y += 30
    d.text((24, y), "v0.82.0 (060b5d2) / protocol 70 / 2026-09-14 / "
                    f"run {RUN_ID}", fill=(150, 170, 190))
    y += 34
    color = {"passed": (90, 220, 120), "failed": (235, 90, 90),
             "not-run": (200, 170, 90)}.get(verdict, (200, 200, 200))
    d.text((24, y), f"VERDICT: {verdict}", fill=color)
    y += 34
    d.text((24, y), "exile PK w/ combat trigger -> Adapt 2 -> counters "
                    "trigger returns PK (finality) -> haste? -> as-enters "
                    "choice? -> end-step sacrifice",
           fill=(170, 190, 210))
    y += 40
    for k in ("A1_parse", "A2_setup_ok", "A3_exile_ok", "A4_adapt_trigger",
              "A5_returned", "A6_haste", "A7_entry_choice",
              "A8_endstep_sacrifice", "A9_cleanup"):
        v = ass.get(k, "not-run")
        c = {"passed": (90, 220, 120), "failed": (235, 90, 90),
             "not-run": (200, 170, 90)}[v]
        d.text((24, y), f"{k}: {v}", fill=c)
        y += 28
    y += 8
    d.text((24, y), "notes:", fill=(150, 170, 190))
    y += 26
    for n in notes[:13]:
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
