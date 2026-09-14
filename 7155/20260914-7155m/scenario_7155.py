#!/usr/bin/env python3
"""phase-rs/phase #7155 - Fiend Artisan: game does not let you pay for
Fiend Artisan's ability.

Oracle (from pinned card-data.json):
  This creature gets +1/+1 for each creature card in your graveyard.
  {X}{B/G}, {T}, Sacrifice another creature: Search your library for a
  creature card with mana value X or less, put it onto the battlefield,
  then shuffle. Activate only as a sorcery.

Card-data AST is faithful: cost = Composite[Mana{shards:[X,BlackGreen]},
Tap, Sacrifice{Typed[Creature]+Another, count 1}]; effect = SearchLibrary
Typed[Creature] with property Cmc LE Ref(Variable X); sub-abilities
ChangeZone Library->Battlefield + Shuffle.

Behavioral contract (2 human seats, native engine):
  P0 fields Fiend Artisan (untapped, past summoning sickness), 2x
  Llanowar Elves fodder, and lands to pay {2}{B/G}. On a P0 main phase
  with an empty stack, P0 activates the ability, answers the X prompt
  with X=2, pays the hybrid mana (auto-tap or prompt), taps, sacrifices
  an Elves, then answers the library search by picking Grizzly Bears
  (MV 2 <= X).
  A1 parse_cost:     card-data cost AST = Mana{X,B/G} + Tap + Sacrifice
                     (Another Creature); effect search carries X-bound Cmc.
  A2 setup_ok:       Artisan on P0 BF untapped & attack-ready; >=1 Elves
                     on P0 BF; >=3 untapped lands (2 generic + B/G).
  A3 activation_offered: ActivateAbility(source=Artisan) advertised to P0
                     on a main phase with empty stack.
  A4 x_prompt:       engine prompts for X; driver answers X=2 and the
                     prompt advances (not stalled).
  A5 cost_paid:      {2}{B/G} + tap + sacrifice all complete: Artisan
                     tapped, one Elves in P0 graveyard, mana spent.
  A6 search_prompt:  library search offered; Bears (MV 2 <= 2) offered.
  A7 resolution:     Bears enters P0 BF; Artisan ability leaves the
                     stack; game proceeds.
  A8 cleanup:        stack empty, no stall, game advanced past the test.

Verdict: reproduced iff A2 passes and any of A3-A7 fails (the reported
"cannot pay for the ability" shape). not-reproduced iff A2-A8 all pass.
blocked iff A2 cannot be established.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7155")
EVDIR = f"{BACKFILL}/evidence/7155/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ARTISAN = "Fiend Artisan"
ELVES = "Llanowar Elves"
BEARS = "Grizzly Bears"
SWAMP, FOREST = "Swamp", "Forest"
LANDS = (SWAMP, FOREST, "Plains", "Island", "Mountain")

P0_DECK = [(ARTISAN, 4), (ELVES, 12), (BEARS, 4),
           (SWAMP, 20), (FOREST, 20)]
P1_DECK = [(FOREST, 60)]
TIMEOUT = 2400
X_VALUE = 2

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
WF_SEEN = []
ACTED = {}
C0 = C1 = None


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "artisan_cast": False,
        "artisan_cast_turn": None,
        "artisan_oid": None,
        "elves_cast": 0,
        "x_prompted": False,
        "x_answered": False,
        "x_confirmed": False,
        "x_last_try": 0.0,
        "activation_offered": False,
        "activation_attempts": 0,
        "activation_submitted": False,
        "activation_accepted": False,
        "offer_scans": 0,
        "sac_target_oid": None,
        "sac_submitted": False,
        "sac_resolved": False,
        "search_prompted": False,
        "search_answered": False,
        "search_choice": None,
        "ability_resolved": False,
        "mana_lands_before": None,
        "mana_lands_after": None,
        "prompt_first_seen": {},
        "unexpected": [],
        "stall_warned": False,
        "mana_taps": 0,
        "mana_spends": 0,
        "mana_sub_rev": None,
        "mana_sub_at": 0.0,
        "finalize_submitted": False,
        "finalize_at": 0.0,
        "finalize_rej_mark": 0,
        "finalize_done": False,
        "finalize_rejected": False,
        "finalize_accepted": False,
        "finalized": False,
    })
    WF_SEEN.clear()


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


def gy_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if oname(o) in LANDS and not o.get("tapped")]


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


# ---------------- viewer_interaction helpers (cf. scenario_7142) ----------
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
    """Card name for a candidate: surface text or referenced object name."""
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
    # post-mulligan bottom-cards: wf stays MulliganDecision but the pending
    # phase is BottomCards and the advertised action is SelectCards
    if wf_type(state) == "MulliganDecision":
        pend = wf_data(state).get("pending", []) or []
        mine = [p for p in pend if p.get("player") == pid]
        if mine and (mine[0].get("phase") or {}).get("type") \
                == "BottomCards":
            n = (mine[0].get("phase") or {}).get("count") or 1
            pick = None
            for sa in acts:
                if sa.get("type") == "SelectCards":
                    oids = sa.get("data", {}).get("cards") or []
                    names = [oname(state["objects"].get(str(x), {}))
                             for x in oids]
                    if oids and all(nm != ARTISAN for nm in names):
                        pick = sa
                        break
            if pick is None:
                oids = [x for x in hand_oids(state, pid)
                        if oname(state["objects"][x]) != ARTISAN][:n]
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
                    if phase == "BottomCards":
                        # (handled above; kept for safety)
                        pass
                    else:
                        choice = "Keep"
                        if pid == 0 and not ST.get("mulliganed"):
                            has_art = any(
                                oname(state["objects"][x]) == ARTISAN
                                for x in hand_oids(state, 0))
                            if not has_art:
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
            if not ST["artisan_cast"]:
                protect |= {ARTISAN}
            if ST["elves_cast"] < 2:
                protect |= {ELVES}
            if not ST["ability_resolved"]:
                protect |= {BEARS}
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
    # leftover SelectCards prompt for this player (non-mulligan shapes):
    # answer with advertised actions, avoiding bottoming the Artisan
    if wf_player(state) == pid:
        for a in acts:
            if a.get("type") == "SelectCards" and not acted(
                    f"selc{pid}", rev):
                oids = a.get("data", {}).get("cards") or []
                names = [oname(state["objects"].get(str(x), {}))
                         for x in oids]
                if oids and all(n != ARTISAN for n in names):
                    await submit_as_is(c, a)
                    say(f"[{c.name}] SelectCards submitted as-is: {a}")
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


# ---------------- cost-prompt handlers ------------------------------------
async def handle_x_value(c, pid, tag, st, state):
    wtype = wf_type(state) or ""
    if "xvalue" not in wtype.lower() and "choose_x" not in wtype.lower():
        return False
    if not wf_player(state) == pid:
        return False
    if ST["x_confirmed"]:
        return False
    if time.time() - ST["x_last_try"] < 5:
        return True
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        spec = ((resp.get("data") or {}).get("spec") or {})
        stype = spec.get("type")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = {"t0": time.time(), "done": False}
            ST["x_prompted"] = True
            wire("x_prompt_shape",
                 {"wf_type": wtype,
                  "wf_data_keys": list(wf_data(state).keys()),
                  "spec": spec})
            say(f"[{tag}] X prompt: spec_type={stype}")
        if stype == "number":
            sub = {"interactionId": iid,
                   "response": {"type": "number",
                                "data": {"value": X_VALUE}}}
            say(f"[{tag}] submitting X={X_VALUE} (number value)")
            wire("interaction_submission",
                 {"who": tag, "submission": sub, "x": X_VALUE})
            await c.send_interaction(sub)
            ST["x_last_try"] = time.time()
            ST["x_answered"] = True
            return True
        chs, rtype = vi_choices(opp)
        if not chs:
            return False
        ST["x_prompted"] = True

        def num(ch):
            for cand in (choice_text(ch), accept_of(ch) or ""):
                try:
                    return int(str(cand).strip())
                except Exception:
                    pass
            return None
        ranked = sorted(((num(ch), ch) for ch in chs),
                       key=lambda t: (t[0] is None,
                                      abs((t[0] or 0) - X_VALUE)))
        best = ranked[0][1]
        say(f"[{tag}] X prompt via choices; picking {choice_text(best)[:60]}")
        await answer_vi(c, opp, best, tag)
        ST["x_last_try"] = time.time()
        ST["x_answered"] = True
        return True
    return False


async def handle_mana_choice(c, pid, tag, st, state):
    """Answer hybrid/colour mana choices (e.g. the {B/G} shard) by picking
    the first Black or Green candidate."""
    wtype = wf_type(state) or ""
    wl = wtype.lower()
    if "mana" not in wl and "color" not in wl and "colour" not in wl:
        return False
    if "xvalue" in wl or "choose_x" in wl:
        return False
    if wf_player(state) != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        spec = ((resp.get("data") or {}).get("spec") or {})
        stype = spec.get("type")
        chs, rtype = vi_choices(opp)
        if not chs:
            return False
        # only answer colour/symbol choices, not tap-selections over lands
        texts = " ".join(choice_text(ch) for ch in chs).lower()
        if stype not in ("manaGroups", "colors", "mana", "choice") and \
                not any(k in texts for k in
                        ("black", "green", "{b}", "{g}", "colour", "color")):
            return False
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = {"t0": time.time(), "done": False}
            wire("mana_prompt_shape",
                 {"wf_type": wtype,
                  "texts": [choice_text(ch)[:40] for ch in chs[:8]]})
            say(f"[{tag}] mana choice prompt: wf={wtype}")
        want = None
        for ch in chs:
            t = (choice_text(ch) + " " + (accept_of(ch) or "")).lower()
            if "black" in t or "{b}" in t or "green" in t or "{g}" in t:
                want = ch
                break
        if want is None:
            want = chs[0]
        say(f"[{tag}] mana choice: picking {choice_text(want)[:60]}")
        await answer_vi(c, opp, want, tag)
        return True
    return False


async def handle_sacrifice(c, pid, tag, st, state):
    """Answer the sacrifice-cost target selection with an Elves."""
    wtype = wf_type(state) or ""
    wl = wtype.lower()
    if ("sacrifice" not in wl and "target" not in wl
            and "paycost" not in wl and "cost" not in wl):
        return False
    if wf_player(state) != pid:
        return False
    if ST["sac_submitted"]:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    fodder = set(bf_named(state, pid, ELVES))
    if ST["artisan_oid"]:
        fodder.discard(str(ST["artisan_oid"]))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = {"t0": time.time(), "done": False}
            wire("sacrifice_prompt_shape",
                 {"wf_type": wtype, "spec": (
                     (opp.get("response", {}) or {}).get("data", {})
                     or {}).get("spec")})
        chs, rtype = vi_choices(opp)
        want = None
        for ch in chs:
            r = ref_of(ch)
            if r is not None and str(r) in fodder:
                want = ch
                break
        if want is None and chs:
            # pick any P0 battlefield creature that is not the Artisan
            for ch in chs:
                r = ref_of(ch)
                o = (state.get("objects") or {}).get(str(r)) if r else None
                if o and o.get("zone") == "Battlefield" \
                        and o.get("controller") == pid \
                        and str(r) != str(ST["artisan_oid"]):
                    want = ch
                    break
        if want is None:
            return False
        ST["sac_target_oid"] = str(ref_of(want))
        say(f"[{tag}] sacrificing oid={ST['sac_target_oid']} "
            f"({choice_text(want)[:60]})")
        await answer_vi(c, opp, want, tag)
        ST["sac_submitted"] = True
        return True
    return False


async def handle_search(c, pid, tag, st, state):
    """Answer the library search by picking Grizzly Bears (MV 2 <= X)."""
    wtype = wf_type(state) or ""
    wl = wtype.lower()
    is_search = ("search" in wl or "select" in wl or "library" in wl
                 or ("target" in wl and ST["sac_resolved"]))
    if not is_search:
        return False
    if wf_player(state) != pid:
        return False
    if ST["search_answered"]:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid not in ST["prompt_first_seen"]:
            ST["prompt_first_seen"][iid] = {"t0": time.time(), "done": False}
            ST["search_prompted"] = True
            wire("search_prompt_shape",
                 {"wf_type": wtype, "spec": (
                     (opp.get("response", {}) or {}).get("data", {})
                     or {}).get("spec")})
            say(f"[{tag}] search prompt seen: wf={wtype}")
        chs, rtype = vi_choices(opp)
        if not chs:
            return False
        ST["search_prompted"] = True
        want = None
        for ch in chs:
            if choice_card_name(ch, state) == BEARS:
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: Bears not among search candidates; "
                f"first 5: {[choice_card_name(ch, state) for ch in chs[:5]]}")
            return False
        ST["search_choice"] = choice_text(want)[:120]
        say(f"[{tag}] search pick: {ST['search_choice']}")
        await answer_vi(c, opp, want, tag)
        ST["search_answered"] = True
        return True
    return False


# ---------------- P0 tick ---------------------------------------------------
async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    rev = st.get("state_revision", -1)

    # --- cost-prompt handlers first: never pass while our cost is pending --
    if await handle_x_value(c, 0, "P0", st, state):
        return True
    if await handle_mana_choice(c, 0, "P0", st, state):
        return True
    if await handle_sacrifice(c, 0, "P0", st, state):
        return True
    if await handle_search(c, 0, "P0", st, state):
        return True

    # confirm X once the wf advances past the X prompt
    if ST["x_answered"] and not ST["x_confirmed"] and wt:
        if "xvalue" not in wt.lower() and "choose_x" not in wt.lower():
            ST["x_confirmed"] = True
            say("[P0] X confirmed (wf advanced past X prompt)")

    # --- resolution watches (every tick) ---------------------------------
    # an activation counts as accepted once any cost prompt (X, mana,
    # sacrifice) or the ability object appears after a submission
    if ST["activation_attempts"] > 0 and not ST["activation_accepted"]:
        if ST["x_prompted"] or ST["sac_submitted"]:
            ST["activation_accepted"] = True
            ST["stage"] = "ACTIVATE"
            say("[P0] activation ACCEPTED (cost prompt seen)")
            wire("activation_accepted", {})
        else:
            for e in state.get("stack") or []:
                desc = json.dumps(e.get("ability") or e)[:400].lower()
                if "fiend artisan" in desc and (
                        e.get("kind") == "ActivatedAbility"
                        or "activated" in desc):
                    ST["activation_accepted"] = True
                    ST["stage"] = "ACTIVATE"
                    say("[P0] activation ACCEPTED (ability on stack)")
                    wire("activation_accepted", {"via": "stack"})
                    break

    if ST["sac_submitted"] and not ST["sac_resolved"] and ST["sac_target_oid"]:
        o = (state.get("objects") or {}).get(str(ST["sac_target_oid"]))
        if o is None or o.get("zone") == "Graveyard":
            ST["sac_resolved"] = True
            say(f"[P0] sacrifice resolved: oid {ST['sac_target_oid']} -> "
                f"{o.get('zone') if o else 'gone'}")

    if ST["activation_submitted"] and not ST["ability_resolved"]:
        bears = bf_named(state, 0, BEARS)
        if bears and ST["search_answered"]:
            ST["ability_resolved"] = True
            say(f"[P0] ability resolved: Bears on P0 BF ({bears})")
            wire("ability_resolved", {"bears": bears})
            await export_now("mid_resolution.json")
            ST["stage"] = "SETTLE"

    if ST["stage"] == "SETTLE" and not ST.get("settle_done"):
        stack = state.get("stack") or []
        if not stack and is_my_main(state, 0):
            ST["settle_done"] = True
            ST["done_reason"] = "ability resolved, stack empty"
            await export_now("post.json")
            ST["stop"] = True
            say("[P0] settle complete")
            return True

    # --- main-phase action taking -----------------------------------------
    if is_my_main(state, 0) and not (state.get("stack") or []):
        # land drop (retry every tick; no keep-flag)
        lo = None
        for lname in (FOREST, SWAMP):
            lo = find_hand(state, 0, lname)
            if lo:
                break
        if lo is None:
            for lname in LANDS:
                lo = find_hand(state, 0, lname)
                if lo:
                    break
        pla = playland_for(acts, lo)
        if pla:
            await submit_as_is(c, pla)
            say(f"[P0] plays land {oname(state['objects'][lo])}")
            return True

        # cast Artisan on schedule
        if not ST["artisan_cast"]:
            ao = find_hand(state, 0, ARTISAN)
            ca = cast_action_for(acts, ao)
            if ca:
                await submit_as_is(c, ca)
                ST["artisan_cast"] = True
                ST["artisan_cast_turn"] = state.get("turn_number")
                say(f"[P0] casts Fiend Artisan (turn "
                    f"{ST['artisan_cast_turn']})")
                return True
        # cast Elves fodder (need >=2 eventually)
        if ST["artisan_cast"] and ST["elves_cast"] < 2:
            eo = find_hand(state, 0, ELVES)
            ce = cast_action_for(acts, eo)
            if ce:
                await submit_as_is(c, ce)
                ST["elves_cast"] += 1
                say(f"[P0] casts Elves #{ST['elves_cast']}")
                return True

        # --- the activation window --------------------------------------
        # Retry every P0 main phase (empty stack): the reported bug is that
        # the advertised activation cannot be paid/initiated. Count
        # offer->submit->reject cycles across turns to rule out a one-tick
        # race. Also try the viewer_interaction activateAbility path if the
        # legacy Action keeps failing.
        artisan = bf_named(state, 0, ARTISAN)
        if artisan:
            ST["artisan_oid"] = artisan[0]
            ao = (state["objects"] or {})[str(artisan[0])]
            ready = (not ao.get("tapped")
                     and state.get("turn_number", 0)
                     > (ST["artisan_cast_turn"] or 0))
            elves = bf_named(state, 0, ELVES)
            unt = len(untapped_lands(state, 0))
            if ready and elves and unt >= X_VALUE + 1 \
                    and wt in ("Priority", None, ""):
                found = None
                for a in acts:
                    if a["type"] == "ActivateAbility" and str(
                            a.get("data", {}).get("source_id")) == str(
                            artisan[0]):
                        found = a
                        break
                # viewer_interaction alternative path?
                vi_act = None
                for opp in vi_opps(st):
                    chs, rtype = vi_choices(opp)
                    for ch in chs:
                        txt = (choice_text(ch) + " "
                               + json.dumps(ch.get("surfaces") or [],
                                            default=str)).lower()
                        if "activateability" in txt or \
                                "activate_ability" in txt:
                            vi_act = (opp, ch)
                            break
                    if vi_act:
                        break
                ST["offer_scans"] += 1
                if ST["offer_scans"] == 1 or ST["offer_scans"] % 10 == 0 \
                        or found:
                    wire("offer_scan",
                         {"scan": ST["offer_scans"],
                          "turn": state.get("turn_number"),
                          "found": bool(found),
                          "action": found,
                          "vi_activate": bool(vi_act),
                          "vi_opp_types": [
                              (o.get("response", {}) or {}).get("type")
                              for o in vi_opps(st)],
                          "untapped_lands": unt,
                          "artisan_tapped": ao.get("tapped")})
                    say(f"[P0] offer scan #{ST['offer_scans']}: "
                        f"offered={bool(found)} vi={bool(vi_act)} "
                        f"untapped_lands={unt}")
                if found and ST["activation_attempts"] < 8:
                    if ST["activation_attempts"] == 0:
                        ST["activation_offered"] = True
                        ST["mana_lands_before"] = unt
                        await export_now("pre_activation.json")
                    if vi_act and ST["activation_attempts"] >= 4:
                        # after 4 legacy rejects, try the vi path once
                        opp, ch = vi_act
                        ST["activation_attempts"] += 1
                        say("[P0] trying viewer_interaction activateAbility "
                            "path")
                        await answer_vi(c, opp, ch, "P0")
                        return True
                    ST["activation_attempts"] += 1
                    ST["activation_submitted"] = True
                    await submit_as_is(c, found)
                    say(f"[P0] ACTIVATION SUBMITTED "
                        f"(attempt {ST['activation_attempts']})")
                    return True
                if ST["activation_attempts"] >= 8 \
                        and not ST.get("giveup_done"):
                    ST["giveup_done"] = True
                    ST["done_reason"] = ("8 activation attempts, all "
                                         "rejected/unresolved")
                    await export_now("post.json")
                    ST["stop"] = True
                    say("[P0] giving up after 8 attempts")
                    return True
        # fall through to the default pass-priority below

    # --- ManaPayment: tap lands, spend pool mana, then finalize -----------
    # (protocol 70: the engine offers tapLandForMana / untapLandForMana /
    #  spendPoolMana / passPriority / cancelCast as exactChoices candidates;
    #  no legacy TapLandForMana Action is advertised. The passPriority
    #  candidate is the engine's own payment-finalize affordance: the
    #  opportunity builder pushes GameAction::PassPriority as the finalize
    #  (CR 601.2h) and the reducer routes PassPriority at ManaPayment to
    #  finalize_mana_payment.)
    if wf_type(state) == "ManaPayment" and wf_player(state) == 0:
        # --- finalize outcome watch: runs before any new submission -----
        if ST.get("finalize_submitted") and not ST.get("finalize_done"):
            new_rej = ST["rejections"][ST.get("finalize_rej_mark", 0):]
            ana = [r for r in new_rej
                   if r.get("type") == "ActionRejected"
                   and (r.get("data", {}).get("rejection", {}) or {}).get(
                       "code") == "action_not_allowed"]
            if ana:
                ST["finalize_done"] = True
                ST["finalize_rejected"] = True
                say("[P0] DECISIVE: the engine-advertised ManaPayment "
                    "finalize (passPriority choice) was rejected "
                    f"action_not_allowed x{len(ana)}; the cost cannot be "
                    "paid, so tap/sacrifice/search never happen")
                wire("finalize_rejected_decisive", {
                    "rejections": ana[:3],
                    "taps": ST.get("mana_taps", 0),
                    "spends": ST.get("mana_spends", 0)})
                await export_now("mid_manapayment.json")
                await export_now("post.json")
                ST["done_reason"] = ("ManaPayment finalize "
                                     "(engine-advertised passPriority) "
                                     "rejected action_not_allowed; cost "
                                     "cannot be paid")
                ST["stop"] = True
                return True
            if time.time() - ST.get("finalize_at", 0) > 20:
                ST["finalize_done"] = True
                say("[P0] finalize submitted 20s ago: no rejection and no "
                    "advance; exporting stuck state")
                wire("finalize_stuck",
                     {"taps": ST.get("mana_taps", 0),
                      "spends": ST.get("mana_spends", 0)})
                await export_now("mid_manapayment.json")
                await export_now("post.json")
                ST["done_reason"] = ("ManaPayment finalize unacknowledged "
                                     "after 20s")
                ST["stop"] = True
                return True
            return True  # keep waiting for the finalize outcome

        # --- revision gate: after any mana submission, wait until the
        # --- revision advances past the submission revision (applied) or
        # --- 15s elapse (rejected/lost) before submitting again. This
        # --- kills the stale same-revision double-submit race.
        if ST.get("mana_sub_rev") is not None:
            if rev <= ST["mana_sub_rev"]:
                if time.time() - ST.get("mana_sub_at", 0) > 15:
                    say("[P0] mana submission unacknowledged for 15s; "
                        "proceeding")
                    wire("mana_sub_timeout",
                         {"rev": rev, "sub_rev": ST["mana_sub_rev"]})
                    ST["mana_sub_rev"] = None
                else:
                    return True
            else:
                ST["mana_sub_rev"] = None  # applied; free to submit again

        taps_needed = X_VALUE + 1  # {X}{B/G}
        # Phase 1: tap lands for mana
        if ST.get("mana_taps", 0) < taps_needed:
            for opp in vi_opps(st):
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    obj = next((s.get("data", {})
                                for s in surfs
                                if s.get("type") == "object"), {})
                    if code == "tapLandForMana" and not obj.get("tapped"):
                        await answer_vi(c, opp, ch, "P0")
                        ST["mana_taps"] = ST.get("mana_taps", 0) + 1
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say(f"[P0] taps {obj.get('name')} for mana "
                            f"(#{ST['mana_taps']}/{taps_needed})")
                        wire("mana_tap",
                             {"n": ST["mana_taps"],
                              "land": obj.get("name"),
                              "choice": ch["id"]})
                        return True
        # Phase 2: spend tapped pool mana toward the cost
        if ST.get("mana_spends", 0) < taps_needed:
            for opp in vi_opps(st):
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    if code == "spendPoolMana":
                        await answer_vi(c, opp, ch, "P0")
                        ST["mana_spends"] = ST.get("mana_spends", 0) + 1
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say(f"[P0] spends pool mana "
                            f"(#{ST['mana_spends']})")
                        wire("mana_spend",
                             {"n": ST["mana_spends"], "choice": ch["id"]})
                        return True
        # Phase 3: confirm the pinned payment via the engine-advertised
        # passPriority finalize. Submitted ONCE; the outcome watch above
        # records the result. Never blindly retried.
        if not ST.get("finalize_submitted"):
            for opp in vi_opps(st):
                chs, rtype = vi_choices(opp)
                if rtype != "exactChoices":
                    continue
                for ch in chs:
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    surfs = ch.get("surfaces") or []
                    code = next((s.get("data", {}).get("code")
                                 for s in surfs
                                 if s.get("type") == "action"), "")
                    if code == "passPriority":
                        await answer_vi(c, opp, ch, "P0")
                        ST["finalize_submitted"] = True
                        ST["finalize_at"] = time.time()
                        ST["finalize_rej_mark"] = len(ST["rejections"])
                        ST["mana_sub_rev"] = rev
                        ST["mana_sub_at"] = time.time()
                        say("[P0] finalize pass submitted "
                            "(engine-advertised passPriority)")
                        wire("mana_finalize", {"choice": ch["id"]})
                        return True
            # no passPriority offered (yet): dump the opportunity once for
            # the record, then keep waiting
            if not ST.get("manapay_dumped"):
                ST["manapay_dumped"] = True
                opps_dump = []
                for opp in vi_opps(st):
                    chs2, rtype2 = vi_choices(opp)
                    opps_dump.append({
                        "interactionId": opp.get("interactionId"),
                        "rtype": rtype2,
                        "n_choices": len(chs2),
                        "choices": [{
                            "id": ch.get("id"),
                            "status": (ch.get("status") or {}).get("type"),
                            "action_code": next(
                                (s.get("data", {}).get("code")
                                 for s in (ch.get("surfaces") or [])
                                 if s.get("type") == "action"), None),
                            "obj_name": next(
                                (s.get("data", {}).get("name")
                                 for s in (ch.get("surfaces") or [])
                                 if s.get("type") == "object"), None),
                            "obj_tapped": next(
                                (s.get("data", {}).get("tapped")
                                 for s in (ch.get("surfaces") or [])
                                 if s.get("type") == "object"), None),
                        } for ch in chs2[:24]]})
                wire("manapay_vi", {
                    "acts": [a.get("type") for a in acts],
                    "mana_taps": ST.get("mana_taps", 0),
                    "wf_data": wf_data(state),
                    "opps": opps_dump})
                say(f"[{c.name}] ManaPayment: no passPriority offered yet; "
                    f"taps={ST.get('mana_taps', 0)} "
                    f"spends={ST.get('mana_spends', 0)} "
                    f"acts={[a['type'] for a in acts]}")
        return True
    # default: pass priority
    pp = find_action(acts, "PassPriority")
    if pp and not acted("pp0", st.get("state_revision", -1)):
        # never pass while our own cost/target prompt is pending
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
        lo = find_hand(state, 1, FOREST)
        if lo is None:
            for lname in LANDS:
                lo = find_hand(state, 1, lname)
                if lo:
                    break
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
    fa = cd["fiend artisan"]
    acts = [a for a in fa["abilities"] if a.get("kind") == "Activated"]
    ok = False
    detail = ""
    if acts:
        a = acts[0]
        cost = a.get("cost") or {}
        costs = cost.get("costs") or []
        kinds = [c.get("type") for c in costs]
        mana = next((c for c in costs if c.get("type") == "Mana"), None)
        shards = ((mana or {}).get("cost") or {}).get("shards") or []
        has_tap = "Tap" in kinds
        sac = next((c for c in costs if c.get("type") == "Sacrifice"), None)
        sac_t = ((sac or {}).get("target") or {})
        sac_props = (sac_t.get("properties") or [])
        sac_ok = (sac_t.get("type_filters") == ["Creature"]
                  and any(p.get("type") == "Another" for p in sac_props))
        eff = a.get("effect") or {}
        filt = eff.get("filter") or {}
        props = filt.get("properties") or []
        cmc = next((p for p in props if p.get("type") == "Cmc"), None)
        cmc_val = (cmc or {}).get("value") or {}
        cmc_qty = cmc_val.get("qty") or {}
        cmc_ok = (eff.get("type") == "SearchLibrary"
                  and filt.get("type") == "Typed"
                  and (cmc or {}).get("comparator") == "LE"
                  and cmc_val.get("type") == "Ref"
                  and cmc_qty.get("type") == "Variable")
        sub = a.get("sub_ability") or {}
        sub_ok = (sub.get("effect") or {}).get("type") == "ChangeZone"
        unimp = "Unimplemented" in json.dumps(a)
        ok = (set(shards) == {"X", "BlackGreen"} and has_tap and sac_ok
              and cmc_ok and sub_ok and not unimp)
        detail = (f"shards={shards} tap={has_tap} sac_ok={sac_ok} "
                  f"cmc_x_bound={cmc_ok} sub_bf={sub_ok} "
                  f"unimplemented={unimp}")
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_check", {"ok": ok, "detail": detail,
                         "ability": acts[0] if acts else None})
    return "passed" if ok else "failed"


def load_state(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())["state"]
    except Exception:
        return None


async def finalize():
    # Idempotent: the scenario deliberately sets ST["stop"] when it has
    # captured the decisive state, but evidence must still be written.
    # A separate "finalized" guard (not "stop") controls this.
    if ST.get("finalized"):
        return
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "timeout"
    say("finalizing...")

    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7155.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass = {}
    notes = []
    a1 = ST.get("a1", "not-run")
    ass["A1_parse_cost"] = a1
    notes.append(f"A1: composite cost Mana{{X,B/G}} + Tap + Sacrifice"
                 f"(Another Creature), search effect X-bound: {a1}")

    pre = load_state("pre_activation.json")
    ok2 = False
    if pre:
        arts = bf_named(pre, 0, ARTISAN)
        ao = (pre.get("objects") or {}).get(str(arts[0])) if arts else None
        elves = bf_named(pre, 0, ELVES)
        unt = len(untapped_lands(pre, 0))
        ok2 = bool(arts) and ao is not None and not ao.get("tapped") \
            and len(elves) >= 1 and unt >= X_VALUE + 1
        notes.append(f"A2: artisan={bool(arts)} tapped={ao.get('tapped') if ao else '?'} "
                     f"elves={len(elves)} untapped_lands={unt}")
    else:
        notes.append("A2: pre_activation.json missing (activation window "
                     "never opened)")
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"

    ok3 = ok2 and ST["activation_offered"]
    ass["A3_activation_offered"] = "passed" if ok3 else (
        "not-run" if not ok2 else "failed")
    notes.append(f"A3: ActivateAbility offered={ST['activation_offered']} "
                 f"(offer scans={ST['offer_scans']}, "
                 f"attempts={ST['activation_attempts']})")

    # A4: the engine must ACCEPT (not reject) the exact advertised
    # activation. Rejection of the verbatim advertised action with
    # action_not_allowed is the reported defect.
    rej = [r for r in ST["rejections"]
           if r.get("type") == "ActionRejected"
           and (r.get("data", {}).get("rejection", {}) or {}).get("code")
           == "action_not_allowed"]
    ok4 = ok3 and ST["activation_accepted"]
    ass["A4_initiation_accepted"] = "passed" if ok4 else (
        "not-run" if not ST["activation_attempts"] else "failed")
    notes.append(f"A4: activation_accepted={ST['activation_accepted']} "
                 f"action_not_allowed_rejects={len(rej)}")

    ok5 = ok4 and ST["x_confirmed"]
    ass["A5_x_prompt"] = "passed" if ok5 else (
        "not-run" if not ST["activation_accepted"] else "failed")
    notes.append(f"A5: x_prompted={ST['x_prompted']} "
                 f"x_answered={ST['x_answered']} "
                 f"x_confirmed={ST['x_confirmed']}")

    ok6 = ok4 and ST["sac_resolved"]
    # cost paid iff: artisan tapped, one elves in gy, sac target resolved
    post = load_state("post.json") or load_state("mid_resolution.json")
    paid_detail = ""
    if post and ST["artisan_oid"]:
        ao = (post.get("objects") or {}).get(str(ST["artisan_oid"]))
        elves_gy = sum(1 for x in gy_oids(post, 0)
                       if oname(post["objects"][x]) == ELVES)
        paid_detail = (f"artisan_tapped={bool(ao and ao.get('tapped'))} "
                       f"elves_in_gy={elves_gy} "
                       f"sac_resolved={ST['sac_resolved']}")
        ok6 = ok5 and ST["sac_resolved"] and bool(
            ao and ao.get("tapped")) and elves_gy >= 1
    else:
        paid_detail = f"sac_resolved={ST['sac_resolved']}"
    ass["A6_cost_paid"] = "passed" if ok6 else (
        "not-run" if not ST["activation_accepted"] else "failed")
    notes.append(f"A6: {paid_detail}")

    ok7 = ok6 and ST["search_prompted"] and ST["search_answered"]
    ass["A7_search_prompt"] = "passed" if ok7 else (
        "not-run" if not ST["sac_resolved"] else "failed")
    notes.append(f"A7: search_prompted={ST['search_prompted']} "
                 f"search_answered={ST['search_answered']} "
                 f"choice={ST['search_choice']}")

    ok8 = ok7 and ST["ability_resolved"]
    bears_bf = False
    if post:
        bears_bf = bool(bf_named(post, 0, BEARS))
        ok8 = ok7 and bears_bf and ST["ability_resolved"]
    ass["A8_resolution"] = "passed" if ok8 else (
        "not-run" if not ST["search_answered"] else "failed")
    notes.append(f"A8: ability_resolved={ST['ability_resolved']} "
                 f"bears_on_p0_bf={bears_bf}")

    post_wf = ((post.get("waiting_for") or {}).get("type")
               if post else None)
    ok9 = (bool(post) and not (post.get("stack") or [])
           and post_wf != "ManaPayment")
    ass["A9_cleanup"] = "passed" if ok9 else (
        "not-run" if not post else "failed")
    notes.append(f"A9: stack empty in post={ok9}; "
                 f"post waiting_for={post_wf}; "
                 f"rejections={len(ST['rejections'])}")

    verdict = "blocked"
    if ok2:
        verdict = "reproduced" if any(
            ass[k] == "failed" for k in
            ("A3_activation_offered", "A4_initiation_accepted",
             "A5_x_prompt", "A6_cost_paid", "A7_search_prompt",
             "A8_resolution")) else "not-reproduced"
    notes.append(f"verdict rule: {verdict}")

    run = {
        "issue": 7155,
        "title": "Fiend Artisan \u2014 Game does not let you pay for "
                 "Fiend Artisan's ability.",
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "validated_version": "v0.82.0",
        "server": SERVER_IDENTITY,
        "scope": "Fiend Artisan activated ability cost chain "
                 "({X}{B/G} + tap + sacrifice another creature) + library "
                 "search, X=2, native engine, two human-client seats",
        "assertions": ass,
        "assertion_notes": notes,
        "verdict": verdict,
        "x_value": X_VALUE,
        "rejections": ST["rejections"],
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "P1 fully passive (60x Forest; plays land, never casts/"
            "attacks/blocks).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"run.json written; verdict={verdict}")

    # ---- summary PNG (derived from saved states + assertions) ------------
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
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #7155 \u2014 Fiend Artisan ability cost",
           fill=(240, 240, 240))
    y += 30
    d.text((24, y), "v0.82.0 (060b5d2) / protocol 70 / 2026-09-14 / "
                    f"run {RUN_ID}", fill=(150, 170, 190))
    y += 34
    color = {"passed": (90, 220, 120), "failed": (235, 90, 90),
             "not-run": (200, 170, 90)}.get(verdict, (200, 200, 200))
    d.text((24, y), f"VERDICT: {verdict}", fill=color)
    y += 34
    d.text((24, y), "Ability: {X}{B/G}, {T}, Sacrifice another creature \u2192 "
                    "search library for creature MV X<=2 \u2192 battlefield",
           fill=(170, 190, 210))
    y += 40
    for k in ("A1_parse_cost", "A2_setup_ok", "A3_activation_offered",
              "A4_initiation_accepted", "A5_x_prompt", "A6_cost_paid",
              "A7_search_prompt", "A8_resolution", "A9_cleanup"):
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
        # stall watchdog: flag if the activation is submitted but nothing
        # advances for 120s (engine stall vs driver stall diagnosis)
        if ST["activation_submitted"] and not ST["ability_resolved"] \
                and not ST["stall_warned"]:
            if ST.get("stall_t0") is None:
                ST["stall_t0"] = time.time()
            if time.time() - ST["stall_t0"] > 120:
                ST["stall_warned"] = True
                say("WATCHDOG: 120s since activation with no resolution")
                wire("watchdog", {"note": "activation in flight, no "
                                  "resolution"})
        await asyncio.sleep(0.05)

    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finalize()


if __name__ == "__main__":
    asyncio.run(main())
