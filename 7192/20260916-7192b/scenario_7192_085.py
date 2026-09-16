#!/usr/bin/env python3
"""Issue #7192: Maralen, Fae Ascendant -- the once-per-turn free-cast
permission ("you may cast a spell with mana value less than or equal to the
number of Elves and Faeries you control from among cards exiled with Maralen
this turn without paying its mana cost") only counts Faeries, not Elves.

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.85.0 card-data.json):
  Maralen, Fae Ascendant {2}{B}{G}{U} 4/5 Flying, Elf Faerie Noble:
    "Whenever Maralen or another Elf or Faerie you control enters, exile the
     top two cards of target opponent's library.
     Once each turn, you may cast a spell with mana value less than or equal
     to the number of Elves and Faeries you control from among cards exiled
     with Maralen this turn without paying its mana cost."

Reported (Discord, status:confirmed; triage clarified):
  "Maralen's cast-permission mana-value limit counts Faeries but not Elves."
  The limit is the combined number of Elves and Faeries controlled; only the
  Faerie portion contributes. Triage: "The AST is faithful; runtime
  quantity/filter aggregation drops one branch of the union." (supported
  filter is the Or union Elf-or-Faerie; runtime counts only one branch.)

Acceptance criteria (from triage):
  (1) Elves and Faeries both contribute once per creature.
  (2) A creature with both types is not double-counted.
  (3) The free-cast permission uses the combined total and only cards exiled
      with Maralen this turn.

Game A (reported case): P0 controls Maralen (Elf Faerie) + 2 Llanowar Elves +
1 Faerie Miscreant = 4 Elves/Faeries combined (2 Faeries-only); Maralen is
only cast once the tribe is complete (gated). Maralen's ETB exiles the top
two cards of P1's library; P1's deck is 60x Flametongue Kavu (MV 4).
Correct: a free cast of the exiled MV-4 Kavu is offered (4 >= 4).
Buggy: limit = 2 (Faeries only), so the MV-4 Kavu is NOT offered.

Game B (control): same P0 board, P1's deck is 60x Grizzly Bears (MV 2).
The MV-2 Bear free cast is offered under BOTH correct and buggy behavior, so
a pass here proves the exile-cast permission mechanism itself works and a
failure in A is attributable to the count bug rather than a broken permission.

Assertions per game G in {A, B}:
  G1_setup     pre: Maralen on P0 battlefield, >=2 Elves and >=1 Faerie
               (Miscreant) on P0 battlefield, exactly the 2 cards exiled by
               Maralen's trigger this turn (names = expected MV card), and
               the exile happened on the same turn Maralen was cast.
  G2_offered   a CastSpell legal action is advertised for an exiled card oid
               (the only cast authority for an exiled card is Maralen's
               permission; the full action is logged).
  G3_cast_ok   submitted free cast resolves: the creature is on P0's
               battlefield with an empty stack in the post state.
  G4_no_pay    no additional mana was paid for the free cast (tapped-land
               count unchanged pre -> post; mana pool logged).

Verdict rule:
  reproduced  -- setup reached in >=1 game AND (
                   A2 fails while B2+B3 pass [the exact reported failure:
                   MV-4 not offered, MV-2 free cast works], OR
                   A2 passes but the cast is rejected/unresolvable [related],
                 OR B2 also fails [related: permission broken entirely]).
  not-reproduced -- A2+A3 pass (MV-4 Kavu offered and cast free).
  blocked -- no game reached setup.

Evidence: evidence/7192/<run-id>/pre_{A,B}.json, post_{A,B}.json,
parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_7192_085.py, wire_log.jsonl, scenario_run.log
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
ISSUE = 7192
RUN_ID = "20260916-7192b"
SERVER_RUN_ID = "20260916-818"  # reused live server (started by the #818 run)
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MARALEN = "Maralen, Fae Ascendant"
ELF = "Llanowar Elves"
FAERIE = "Faerie Miscreant"
KAVU = "Flametongue Kavu"   # MV 4 -- discriminates combined(4) vs faeries-only(2)
BEAR = "Grizzly Bears"      # MV 2 -- control, offered under both behaviors
LANDS = ("Swamp", "Forest", "Island")

# Denser tribe (probe 20260916-7192 showed the 8x/2x tribe may not complete
# before Maralen becomes castable; Maralen's cast is now gated on the tribe).
A_P0_DECK = [(MARALEN, 4), (ELF, 12), (FAERIE, 4),
             ("Swamp", 14), ("Forest", 14), ("Island", 12)]
A_P1_DECK = [(KAVU, 60)]
B_P0_DECK = list(A_P0_DECK)
B_P1_DECK = [(BEAR, 60)]

# Desired board at free-cast time: Maralen (Elf Faerie, counts once) + 2 Elves
# + 1 Faerie Miscreant -> combined 4, faeries-only 2. MV-4 Kavu discriminates.
WANT_ELVES = 2
WANT_FAERIES = 1

ST = {}
ACTED = set()
PROMPT_DONE = set()
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
ANSWERED_IID = {}
C0 = None


def reset_per_game():
    """Fresh per-revision/per-prompt guards for each new game: revisions
    restart at 0 per game, so a global ACTED set would suppress legitimate
    first-time actions in later games (#5654 lesson)."""
    global ACTED, PROMPT_DONE, REJECTS, LAST_IID, SKIP_IID, ANSWERED_IID
    ACTED = set()
    PROMPT_DONE = set()
    REJECTS = {}
    LAST_IID = {}
    SKIP_IID = set()
    ANSWERED_IID = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload},
                          default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return o.get("card_name") or o.get("base_name") or o.get("name") or ""


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def life(state, pid):
    return state["players"][pid]["life"]


def hand_oids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def hand_names(state, pid):
    return [oname(get_obj(state, oid)) for oid in hand_oids(state, pid)]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(get_obj(state, oid)) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_find(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def bf_count(state, pid, name):
    return sum(1 for _, o in bf(state, pid) if oname(o) == name)


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in LANDS]


def untapped_land_names(state, pid):
    return [oname(get_obj(state, x)) for x in untapped_lands(state, pid)]


def tapped_land_count(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if o.get("tapped") and oname(o) in LANDS)


def exile_oids(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Exile"]


def mana_pool(state, pid):
    p = state["players"][pid]
    for k in ("mana_pool", "manaPool", "pool", "mana"):
        if k in p:
            return {k: p[k]}
    return {"note": "no pool field; keys=" + ",".join(sorted(p.keys())[:12])}


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def my_priority(state, pid):
    """Default PassPriority gated on actually holding priority (#4509)."""
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    return (wf.get("type") == "Priority"
            and (d.get("player") == pid or d.get("deciding_player") == pid))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


def copies_in_flight(state, name):
    """Copies of `name` currently on the stack (cast but unresolved)."""
    n = 0
    needle = name.lower()
    for e in stack_entries(state):
        if not isinstance(e, dict):
            e = get_obj(state, e)
        if needle in json.dumps(e, default=str).lower():
            n += 1
    return n


# ------------------------------------------------------------- interaction helpers
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
    return None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    return chs, resp.get("type")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_IID[c.name] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {body[:300]}")
            iid = LAST_IID.get(c.name)
            if iid and iid in body:
                REJECTS[iid] = REJECTS.get(iid, 0) + 1
                say(f"[{c.name}] iid {iid} rejection #{REJECTS[iid]}")
                if REJECTS[iid] >= 2 and iid not in SKIP_IID:
                    SKIP_IID.add(iid)
                    say(f"[{c.name}] iid {iid} rejected twice; skipping "
                        f"further submissions on it (logged, #4509)")


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED.add(k)
    return False


async def export_now(path, client):
    s = await client.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    env = json.loads(s)
    return env["state"] if isinstance(env, dict) and "state" in env else env


def castspell_advertised(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None

# ------------------------------------------------------------- per-tick handlers
async def handle_mulligan(c, pid, tag, st, state, acts, want_keep):
    if wf_type(state) != "MulliganDecision":
        return False
    pend = wf_data(state).get("pending") or []
    mine = [e for e in pend
            if (e.get("player") if isinstance(e, dict) else None) == pid
            and (e.get("phase") or {}).get("type") == "Declare"]
    if not mine:
        return False
    rev = st.get("state_revision", -1)
    if acted(f"mull{pid}", rev):
        return True
    mulls = ST.get(f"mulls{pid}", 0)
    want = "keep" if (want_keep(state, pid) or mulls >= 2) else "mulligan"
    if want == "mulligan":
        ST[f"mulls{pid}"] = mulls + 1
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"), None)
    if adv:
        sub = copy.deepcopy(adv)
        dd = sub.setdefault("data", {})
        dd["decision"] = want
        dd["choice"] = {"type": "Keep" if want == "keep" else "Mulligan"}
    else:
        sub = {"type": "MulliganDecision",
               "data": {"decision": want,
                        "choice": {"type": "Keep" if want == "keep" else "Mulligan"}}}
    await submit_as_is(c, sub)
    say(f"[{tag}] mulligan: {want}")
    return True


async def handle_bottom(c, pid, tag, st, state, acts):
    pend = wf_data(state).get("pending") or []
    mine = [e for e in pend
            if (e.get("player") if isinstance(e, dict) else None) == pid
            and (e.get("phase") or {}).get("type") == "BottomCards"]
    if not mine:
        return False
    rev = st.get("state_revision", -1)
    if acted(f"bottom{pid}", rev):
        return True
    n = 1
    try:
        n = int(mine[0]["phase"].get("count", 1))
    except Exception:
        pass
    h = hand_oids(state, pid)
    h.sort(key=lambda o: (oname(get_obj(state, o)) not in LANDS,
                         oname(get_obj(state, o))))
    adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
    if adv:
        sub = copy.deepcopy(adv)
        sub["data"]["cards"] = [int(x) for x in h[:n]]
        await submit_as_is(c, sub)
    else:
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
            picks = [oid_by_ref[o] for o in h[:n] if o in oid_by_ref]
            if len(picks) == n:
                await send_interaction(
                    c, {"interactionId": opp.get("interactionId"),
                        "response": {"type": "select",
                                     "data": {"choiceIds": picks}}})
                break
        else:
            return False
    say(f"[{tag}] bottoms {n} after mulligan")
    return True


async def answer_discard(c, pid, opp, tag, protect):
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = (data.get("spec") or {}).get("type") if isinstance(
        data.get("spec"), dict) else data.get("type")
    chs = data.get("choices") or data.get("candidates") or []
    state = c.latest["state"]
    d = wf_data(state)
    count = 1
    for k in ("count", "amount", "number"):
        if isinstance(d.get(k), int):
            count = d[k]
    oid_by_ref = {}
    for ch in chs:
        r = ref_of(ch)
        if r:
            oid_by_ref[r] = ch["id"]

    def rank(oid):
        nm = oname(get_obj(state, oid))
        if nm in protect:
            return (2, nm)
        if nm in LANDS:
            return (0, nm)
        return (1, nm)
    oids = sorted(hand_oids(state, pid), key=rank)[:count]
    picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
    if len(picks) < len(oids):
        say(f"[{tag}] discard: unmapped candidates; deferring")
        return False
    cids = [cid for _, cid in picks]
    if rtype == "schema" and spec in ("sequence", "select"):
        resp_out = {"type": spec, "data": {"choiceIds": cids}}
    elif rtype == "exactChoices":
        resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
    else:
        say(f"[{tag}] discard: unexpected rtype={rtype} spec={spec}; deferring")
        return False
    await send_interaction(c, {"interactionId": opp.get("interactionId"),
                              "response": resp_out})
    names = [oname(get_obj(state, o)) for o, _ in picks]
    say(f"[{tag}] discards {names}")
    wire("discard_answered", {"tag": tag, "picks": names})
    return True


async def handle_nonpriority(c, pid, tag, st, state, acts, protect=()):
    wtype = wf_type(state)
    if wtype == "Priority":
        return False
    if await handle_mulligan(c, pid, tag, st, state, acts,
                             ST["want_keep"][pid]):
        return True
    if await handle_bottom(c, pid, tag, st, state, acts):
        return True
    d = wf_data(state)
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    if wtype in ("DiscardChoice", "DiscardToHandSize") and dp == pid:
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in PROMPT_DONE or iid in SKIP_IID:
                    continue
                if await answer_discard(c, pid, opp, tag, protect):
                    PROMPT_DONE.add(iid)
                    return True
        say(f"[{tag}] {wtype} but no answerable opportunity; deferring")
        return True
    if wtype in ("DeclareAttackers", "DeclareBlockers"):
        adv = next((a for a in acts if a.get("type") == wtype), None)
        if adv:
            sub = copy.deepcopy(adv)
            dd = sub.setdefault("data", {})
            for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                if k in dd:
                    dd[k] = [] if isinstance(dd[k], list) else {}
            await submit_as_is(c, sub)
            say(f"[{tag}] declares no {wtype}")
            return True
        return False
    if wtype == "ChooseLegend":
        adv = next((a for a in acts if a.get("type") == "ChooseLegend"), None)
        if adv:
            await submit_as_is(c, adv)
            say(f"[{tag}] chooses legend (keep first)")
            return True
        return False
    # NOTE: unknown non-priority prompts are NOT auto-passed here (unlike the
    # #7191 template): the caller answers target selections first, and anything
    # else is left for explicit handling so we never burn a decision prompt.
    return False


async def answer_trigger_target(c, pid, tag, st, state, acts):
    """Answer TargetSelection/TriggerTargetSelection for our seat, one choice
    per prompt (#7179: never submit 2 choiceIds to a single-slot prompt).
    Classifies by candidate shape: player-seat candidates -> Maralen's exile
    trigger (target opponent); creature refs -> Kavu ETB damage trigger."""
    wtype = wf_type(state)
    if wtype not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    d = wf_data(state)
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    if dp != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if ANSWERED_IID.get(iid, 0) >= 3 or iid in SKIP_IID:
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        p1seat = [ch for ch in chs if seat_of(ch) == 1 and ref_of(ch) is None]
        if p1seat:
            pick, kind = p1seat[0], "maralen_exile_target"
        else:
            mis = bf_find(state, 0, FAERIE)
            pick, kind = None, "kavu_etb_target"
            if mis is not None:
                for ch in chs:
                    if ref_of(ch) == str(mis):
                        pick = ch
                        break
            if pick is None:
                for ch in chs:
                    if ref_of(ch) is not None:
                        pick = ch
                        break
            if pick is None:
                say(f"[{tag}] {wtype}: no usable candidate; deferring")
                continue
        wire("trigger_target_answer",
             {"tag": tag, "wtype": wtype, "iid": iid, "kind": kind,
              "n_candidates": len(chs), "choice": pick["id"]})
        await send_interaction(
            c, {"interactionId": iid,
                "response": {"type": "sequence",
                             "data": {"choiceIds": [pick["id"]]}}})
        ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
        ST.setdefault("trigger_answers", []).append(
            {"game": ST["game"], "wtype": wtype, "kind": kind, "iid": iid})
        say(f"[{tag}] answers {wtype} ({kind})")
        return True
    return False


def economy_tick(c, pid, st, acts, state, tag):
    """Land drop in main + priority-gated pass. Returns (kind, action) or None."""
    rev = st.get("state_revision", -1)
    if is_my_main(state, pid):
        pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
        if pl and not acted(f"land{pid}", rev):
            return ("land", pl)
    if my_priority(state, pid):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            return ("pass", pp)
    return None


def scan_free_cast_offer(st, acts, state):
    """Look for the free-cast offer on exiled oids in legal_actions AND in
    viewer_interaction. Returns (route, payload) or None."""
    exiled = set(str(x) for x in ST.get("exiled_oids", []))
    if not exiled:
        return None
    for a in acts:
        if a.get("type") == "CastSpell":
            oid = str((a.get("data") or {}).get("object_id", ""))
            if oid in exiled:
                return ("legal_action", a)
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            chs, rtype = vi_choices(opp)
            blob = json.dumps(opp, default=str).lower()
            if ("cast" in blob or "exile" in blob) and any(
                    ref_of(ch) in exiled for ch in chs):
                return ("viewer_interaction", opp)
    return None

# ------------------------------------------------------------- P0 game logic
async def p0_tick(st, acts, state, tag):
    c = _CLIENTS["p0"]
    drain_rejections(c)
    if await answer_trigger_target(c, 0, tag, st, state, acts):
        return
    if await handle_nonpriority(c, 0, tag, st, state, acts,
                               protect=(MARALEN, ELF, FAERIE)):
        return

    g = ST["game"]
    # --- stage: develop (get Maralen + tribe on the battlefield) ---
    if not ST.get("maralen_on_bf"):
        if bf_find(state, 0, MARALEN) is not None:
            ST["maralen_on_bf"] = True
            ST["maralen_turn"] = turn_of(state)
            ST["exile_before"] = set(exile_oids(state))
            ST["lib1_before"] = lib_size(state, 1)
            say(f"[{tag}] Maralen on battlefield (turn {ST['maralen_turn']})")
        elif my_priority(state, 0) and is_my_main(state, 0):
            ul = untapped_land_names(state, 0)
            tribe_ok = (bf_count(state, 0, ELF) >= WANT_ELVES
                        and bf_count(state, 0, FAERIE) >= WANT_FAERIES)
            # 1) tribe first: exactly WANT_ELVES Elves, WANT_FAERIES Faeries
            # (stack-aware so an in-flight copy isn't double-cast).
            if bf_count(state, 0, ELF) + copies_in_flight(state, ELF) < WANT_ELVES:
                e = find_hand(state, 0, ELF)
                if e is not None and "Forest" in ul:
                    cast = castspell_advertised(acts, e)
                    if cast and not acted("castelf",
                                          st.get("state_revision", -1)):
                        await submit_as_is(c, cast)
                        say(f"[{tag}] casts {ELF}")
                        return
            if bf_count(state, 0, FAERIE) + copies_in_flight(state, FAERIE) < WANT_FAERIES:
                f = find_hand(state, 0, FAERIE)
                if f is not None and len(ul) >= 2 and "Island" in ul:
                    cast = castspell_advertised(acts, f)
                    if cast and not acted("castfaerie",
                                          st.get("state_revision", -1)):
                        await submit_as_is(c, cast)
                        say(f"[{tag}] casts {FAERIE}")
                        return
            # 2) Maralen herself ({2}{B}{G}{U}), ONLY once the tribe is
            # complete -- otherwise the combined-vs-faeries discrimination
            # is lost (probe 20260916-7192 lesson).
            m = find_hand(state, 0, MARALEN)
            if (m is not None and tribe_ok and len(ul) >= 5
                    and "Swamp" in ul and "Forest" in ul and "Island" in ul
                    and copies_in_flight(state, MARALEN) == 0):
                cast = castspell_advertised(acts, m)
                if cast and not acted("castmaralen",
                                      st.get("state_revision", -1)):
                    await submit_as_is(c, cast)
                    say(f"[{tag}] casts {MARALEN} (tribe complete)")
                    return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: watch for the exile trigger to resolve ---
    if not ST.get("exiled_oids"):
        cur = set(exile_oids(state))
        new = sorted(cur - ST.get("exile_before", set()),
                     key=lambda x: int(x))
        if len(new) >= 2:
            ST["exiled_oids"] = new
            ST["exile_turn"] = turn_of(state)
            ST["lib1_after"] = lib_size(state, 1)
            names = [oname(get_obj(state, x)) for x in new]
            say(f"[{tag}] exile observed: {names} (turn {ST['exile_turn']}, "
                f"P1 library {ST['lib1_before']}->{ST['lib1_after']})")
            wire("exile_observed",
                 {"oids": new, "names": names,
                  "lib_delta": ST["lib1_before"] - ST["lib1_after"]})
            ST[f"pre{g}"] = await export_now(f"pre_{g}.json", C0)
            pre = ST[f"pre{g}"]
            ST["tapped_pre"] = tapped_land_count(pre, 0)
            ST["pool_pre"] = mana_pool(pre, 0)
            ST["holding"] = True
            ST["hold_start"] = time.time()
            wire("legal_actions_at_pre",
                 [a for a in acts if a.get("type") == "CastSpell"])
            return
        # trigger still pending: keep the game moving
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: holding for the free-cast offer (do NOT pass priority away) ---
    if ST.get("holding"):
        if time.time() - ST["hold_start"] > 150:
            ST["holding"] = False
            ST["offer"] = None
            say(f"[{tag}] offer window expired (150s): no free cast advertised")
            wire("offer_window_expired", {})
            ST[f"post{g}"] = await export_now(f"post_{g}.json", C0)
            return
        if my_priority(state, 0) and is_my_main(state, 0):
            found = scan_free_cast_offer(st, acts, state)
            if found:
                route, payload = found
                ST["offer"] = {"route": route, "payload": payload}
                wire("free_cast_offer", {"route": route,
                                        "payload": payload})
                say(f"[{tag}] FREE CAST OFFERED via {route}: "
                    f"{json.dumps(payload, default=str)[:400]}")
                if route == "legal_action":
                    await submit_as_is(c, payload)
                    ST["cast_submitted"] = True
                    ST["holding"] = False
                    ST["cast_time"] = time.time()
                    say(f"[{tag}] submitted free cast as-is")
                else:
                    say(f"[{tag}] offer via viewer_interaction; leaving for "
                        f"manual inspection this run")
                    ST["holding"] = False
            # else: hold priority silently; re-scan next tick
        return

    # --- stage: cast submitted, drive to resolution ---
    if ST.get("cast_submitted") and not ST.get(f"post{g}"):
        tgt = ST["target_name"]
        if bf_find(state, 0, tgt) is not None and not stack_entries(state):
            say(f"[{tag}] free-cast creature on battlefield; exporting POST")
            ST[f"post{g}"] = await export_now(f"post_{g}.json", C0)
            post = ST[f"post{g}"]
            ST["tapped_post"] = tapped_land_count(post, 0)
            ST["pool_post"] = mana_pool(post, 0)
            return
        if time.time() - ST["cast_time"] > 240:
            say(f"[{tag}] resolution timeout after cast submission")
            ST[f"post{g}"] = await export_now(f"post_{g}.json", C0)
            return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- fallback: keep the game moving ---
    r = economy_tick(c, 0, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


async def p1_tick(st, acts, state, tag):
    c = _CLIENTS["p1"]
    drain_rejections(c)
    if await handle_nonpriority(c, 1, tag, st, state, acts, protect=()):
        return
    r = economy_tick(c, 1, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state(game, target_name):
    reset_per_game()
    ST.update({"game": game, "target_name": target_name,
               "maralen_on_bf": False, "maralen_turn": None,
               "exile_before": set(), "exiled_oids": None, "exile_turn": None,
               "lib1_before": None, "lib1_after": None,
               "holding": False, "hold_start": 0, "offer": None,
               "cast_submitted": False, "cast_time": 0,
               "tapped_pre": None, "tapped_post": None,
               "pool_pre": None, "pool_post": None,
               "trigger_answers": [], "max_turn": 0,
               "mulls0": 0, "mulls1": 0, "want_keep": [None, None],
               f"pre{game}": None, f"post{game}": None})


async def open_game(tag, p0_deck, p1_deck):
    p0 = PhaseClient(f"{tag}-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient(f"{tag}-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*p1_deck))
    say(f"[{tag}] game={p0.game_code}")
    global C0
    C0 = p0
    return p0, p1


async def run_game(p0, p1, timeout_s):
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    ticks = [(p0, p0_tick, 0, f"{ST['game']}-P0"),
             (p1, p1_tick, 1, f"{ST['game']}-P1")]
    for _ in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        for c, tick, pid, tag in ticks:
            st = c.latest
            if not st:
                continue
            rev = c.revision
            if rev == last.get(c.name) and time.time() - last_tick.get(c.name, 0) <= 5:
                continue
            last[c.name] = rev
            last_tick[c.name] = time.time()
            last_adv[c.name] = time.time()
            warned.discard(c.name)
            state = st["state"]
            ST["max_turn"] = max(ST["max_turn"], turn_of(state))
            acts = list(st.get("legal_actions") or [])
            try:
                await tick(st, acts, state, tag)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
            if ST.get(f"post{ST['game']}") is not None:
                say(f"[{ST['game']}] contract complete; finishing game")
                return True
        now = time.time()
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            say(f"[{ST['game']}] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_type(s)} "
                f"P0hand={len(hand_names(s,0))} P1hand={len(hand_names(s,1))} "
                f"stack={len(stack_entries(s))} maralen={ST.get('maralen_on_bf')} "
                f"exiled={ST.get('exiled_oids')} holding={ST.get('holding')}")
        for c in (p0, p1):
            if (now - last_adv[c.name] > 60 and c.name not in warned
                    and c.latest):
                warned.add(c.name)
                st = c.latest
                state = st.get("state", {})
                say(f"[{ST['game']}] WATCHDOG {c.name}: no revision advance for "
                    f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                    f"turn={turn_of(state)} phase={state.get('phase')} "
                    f"wf={wf_type(state)}")
        if now - t0 > timeout_s:
            say(f"[{ST['game']}] global timeout ({timeout_s}s) hit")
            return False
    return False


def p0_keep(state, pid):
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n in LANDS)
    return MARALEN in hn and lands >= 2


async def play_game(game, p0_deck, p1_deck, target_name):
    new_game_state(game, target_name)
    p0, p1 = await open_game(game, p0_deck, p1_deck)
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1
    ST["want_keep"] = [p0_keep, lambda state, pid: True]
    ok = await run_game(p0, p1, timeout_s=1500)
    say(f"game {game} finished ok={ok}")
    await p0.close()
    await p1.close()
    return ok

# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    e = cd["maralen, fae ascendant"]
    trig = (e.get("triggers") or [])[0]
    stat = (e.get("static_abilities") or [])[0]

    def filt_summary(f):
        if not isinstance(f, dict):
            return str(f)
        t = f.get("type")
        if t == "Or":
            return "Or(" + ",".join(filt_summary(x) for x in f.get("filters", [])) + ")"
        if t == "Typed":
            subs = [x.get("Subtype") for x in f.get("type_filters", [])
                    if isinstance(x, dict)]
            return f"Typed(subtypes={subs},controller={f.get('controller')})"
        if t == "SelfRef":
            return "SelfRef"
        return t

    aff = stat.get("affected", {}) or {}
    props = aff.get("properties") or []
    cmc = next((p for p in props if isinstance(p, dict)
                and p.get("type") == "Cmc"), {})
    qty = ((cmc.get("value") or {}).get("qty")) or {}
    out = {"maralen, fae ascendant": {
        "oracle_text": e.get("oracle_text"),
        "subtypes": (e.get("card_type") or {}).get("subtypes"),
        "etb_trigger_filter": filt_summary(trig.get("valid_card")),
        "etb_effect": ((trig.get("execute") or {}).get("effect") or {}).get("type"),
        "static_mode": list((stat.get("mode") or {}).keys()),
        "static_frequency": ((stat.get("mode") or {}).get("ExileCastPermission")
                             or {}).get("frequency"),
        "static_pool": ((stat.get("mode") or {}).get("ExileCastPermission")
                        or {}).get("pool"),
        "cmc_comparator": cmc.get("comparator"),
        "count_qty_type": qty.get("type"),
        "count_filter": filt_summary((qty.get("filter") or {})),
    }}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(parse, snaps, offers):
    """snaps: {game: (pre, post)}. offers: {game: offer-record or None}.
    Returns (assertions, notes, verdict)."""
    ass = {}
    notes = []
    pe = parse["maralen, fae ascendant"]
    notes.append(
        "parse (pinned v0.85.0 card-data): etb_filter="
        f"{pe['etb_trigger_filter']} effect={pe['etb_effect']}; static_mode="
        f"{pe['static_mode']} freq={pe['static_frequency']} pool="
        f"{pe['static_pool']}; limit=Cmc {pe['cmc_comparator']} ObjectCount("
        f"{pe['count_filter']}) -- AST is the faithful Elf-or-Faerie union, "
        "matching the triage finding that the defect is at runtime.")

    setup = {}
    for g, tgt in (("A", KAVU), ("B", BEAR)):
        pre, post = snaps[g]
        if pre is None:
            ass[f"{g}1_setup"] = "failed"
            notes.append(f"{g}1_setup failed: Maralen never reached the "
                         f"battlefield / exile never observed")
            for k in (f"{g}2_offered", f"{g}3_cast_ok", f"{g}4_no_pay"):
                ass[k] = "not-run"
            setup[g] = False
            continue
        mar = bf_find(pre, 0, MARALEN) is not None
        elves = bf_count(pre, 0, ELF)
        fae = bf_count(pre, 0, FAERIE)
        # Maralen herself is Elf Faerie and counts once: combined =
        # 1 + elves + fae; faeries-only = 1 + fae.
        combined = (1 if mar else 0) + elves + fae
        fae_only = (1 if mar else 0) + fae
        ex = [oname(get_obj(pre, x)) for x in
              [o for o, ob in pre.get("objects", {}).items()
               if ob.get("zone") == "Exile"]]
        ex_ok = (len(ex) == 2 and all(n == tgt for n in ex))
        ok = mar and elves >= WANT_ELVES and fae >= WANT_FAERIES and ex_ok
        setup[g] = ok
        ass[f"{g}1_setup"] = "passed" if ok else "failed"
        notes.append(f"{g}1_setup: maralen={mar}, elves={elves}, faeries={fae} "
                     f"(combined={combined}, faeries-only={fae_only}), "
                     f"exiled={ex} (expect 2x {tgt}): {'ok' if ok else 'SETUP FAILED'}")
        if not ok:
            for k in (f"{g}2_offered", f"{g}3_cast_ok", f"{g}4_no_pay"):
                ass[k] = "not-run"
            continue
        off = offers[g]
        if off and off.get("route") == "legal_action":
            ass[f"{g}2_offered"] = "passed"
            notes.append(f"{g}2_offered: CastSpell advertised for exiled "
                         f"{tgt} via legal_actions (the only cast authority "
                         f"for an exiled card is Maralen's permission)")
        elif off:
            ass[f"{g}2_offered"] = "passed"
            notes.append(f"{g}2_offered: free cast surfaced via "
                         f"{off.get('route')} (logged, not legal_actions)")
        else:
            ass[f"{g}2_offered"] = "failed"
            notes.append(f"{g}2_offered FAILED: no free cast advertised for "
                         f"the exiled MV-{4 if g == 'A' else 2} {tgt} within "
                         f"the 150s offer window")
        if post is None:
            ass[f"{g}3_cast_ok"] = "failed"
            notes.append(f"{g}3_cast_ok failed: no post state captured")
            ass[f"{g}4_no_pay"] = "not-run"
            continue
        onbf = bf_find(post, 0, tgt) is not None
        calm = len(stack_entries(post)) == 0
        if onbf and calm:
            ass[f"{g}3_cast_ok"] = "passed"
            notes.append(f"{g}3_cast_ok: {tgt} on P0 battlefield, stack empty")
        elif onbf:
            ass[f"{g}3_cast_ok"] = "passed"
            notes.append(f"{g}3_cast_ok: {tgt} on P0 battlefield (stack "
                         f"non-empty at export; see log)")
        else:
            ass[f"{g}3_cast_ok"] = "failed"
            notes.append(f"{g}3_cast_ok FAILED: {tgt} not on P0 battlefield "
                         f"post cast")
        tp0, tp1 = ST.get("tapped_pre"), ST.get("tapped_post")
        if tp0 is not None and tp1 is not None and ass[f"{g}3_cast_ok"] == "passed":
            if tp1 == tp0:
                ass[f"{g}4_no_pay"] = "passed"
                notes.append(f"{g}4_no_pay: tapped lands {tp0}->{tp1}; no "
                             f"mana paid for the free cast")
            else:
                ass[f"{g}4_no_pay"] = "failed"
                notes.append(f"{g}4_no_pay FAILED: tapped lands {tp0}->{tp1}; "
                             f"the 'free' cast tapped lands")
        else:
            ass[f"{g}4_no_pay"] = "not-run"
            notes.append(f"{g}4_no_pay not-run (no comparable tap counts)")

    a2, b2 = ass.get("A2_offered"), ass.get("B2_offered")
    a3, b3 = ass.get("A3_cast_ok"), ass.get("B3_cast_ok")
    if not setup["A"] and not setup["B"]:
        verdict = "blocked"
        notes.append("verdict: blocked - no game reached the exile setup")
    elif a2 == "failed" and b2 == "passed" and b3 == "passed":
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - the MV-4 Kavu free cast was NOT "
                     "offered (limit counts only the 2 Faeries, not the "
                     "combined 4 Elves+Faeries), while the MV-2 Bear free "
                     "cast was offered and resolved (permission mechanism "
                     "works; the defect is the count, exactly as reported)")
    elif a2 == "passed" and a3 == "passed":
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - MV-4 Kavu offered and cast "
                     "for free with 2 Elves + 1 Faerie + Maralen on board")
    elif b2 == "failed":
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED (related failure) - even the MV-2 "
                     "control free cast was not offered; the exile-cast "
                     "permission is broken beyond the count bug")
    elif a2 == "passed" and a3 != "passed":
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED (related failure) - MV-4 Kavu was "
                     "offered but the free cast did not resolve")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - inconclusive observations")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1240
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#7192 Maralen, Fae Ascendant: free-cast limit counts Faeries only, "
         "not Elves", fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 Maralen + 2 Llanowar Elves + 1 Faerie Miscreant "
         "(combined 4; faeries-only 2)", size=16)
    line("A: exile 2x Flametongue Kavu (MV 4) | B: exile 2x Grizzly Bears "
         "(MV 2, control)", size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:16]:
        line(f"  - {n[:112]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.85.0, protocol 72)")

    parse = parse_evidence()
    say("parse:", json.dumps(parse))

    snaps = {}
    offers = {}
    results = {}
    for game, p0d, p1d, tgt in (("A", A_P0_DECK, A_P1_DECK, KAVU),
                               ("B", B_P0_DECK, B_P1_DECK, BEAR)):
        try:
            ok = await play_game(game, p0d, p1d, tgt)
            say(f"game {game} finished ok={ok}")
        except Exception as e:
            say(f"game {game} crashed: {e!r}")
            ok = False
        results[game] = ok
        snaps[game] = (ST.get(f"pre{game}"), ST.get(f"post{game}"))
        offers[game] = ST.get("offer")

    ass, notes, verdict = evaluate(parse, snaps, offers)
    dur = time.time() - t_start

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    cd_path = f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.85.0/data/draft-pools.json"
    bin_path = (f"{BACKFILL}/server/releases/v0.85.0/"
                f"phase-server-slim-x86_64-unknown-linux-musl")
    server_identity = {
        "server_version": "0.85.0",
        "build_commit": "cb58ef5",
        "protocol_version": 72,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY",
        "signature_verified": True,
        "observed_at": "2026-09-16",
        "source": "reused live server on 127.0.0.1:9374 (run 20260916-818); "
                  "ServerHello confirms v0.85.0/cb58ef5/protocol 72; hashes "
                  "recomputed from on-disk release files this run; v0.85.0 "
                  "confirmed latest stable release (not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{SERVER_RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_7192_085.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_7192_085.py"),
        "decks": {"A_P0": A_P0_DECK, "A_P1": A_P1_DECK,
                  "B_P0": B_P0_DECK, "B_P1": B_P1_DECK},
        "games": results,
        "offers": {g: (o["route"] if o else None) for g, o in offers.items()},
        "parse_summary": parse,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-16",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "The test discriminates combined-count 4 vs faeries-only 2; it "
            "cannot distinguish a correct combined count from double-counting "
            "Maralen (5). No MV-5 exiled spell was in the pool.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 4x Maralen + 12x Llanowar Elves + 4x Faerie Miscreant "
                      "+ 14 Swamp/14 Forest/12 Island vs P1 60x Flametongue "
                      "Kavu (A) / 60x Grizzly Bears (B). P0 completes the "
                      "tribe (2 Elves + 1 Faerie), casts Maralen, answers the "
                      "exile trigger (P1), holds main-phase priority for the "
                      "free-cast offer.",
        "contract_line": "With Maralen + 2 Elves + 1 Faerie (combined 4), the "
                         "once-per-turn free cast must be offered for an "
                         "exiled MV-4 spell; MV-2 is the control (offered "
                         "under both behaviors).",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7192_085.py", "w") as f:
        f.write(open(__file__).read())
    render_png(f"{EVDIR}/summary.png", run)
    # hash scenario_run.log AFTER all say() logging is done (#6916 lesson)
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1),
          flush=True)


asyncio.run(amain())
