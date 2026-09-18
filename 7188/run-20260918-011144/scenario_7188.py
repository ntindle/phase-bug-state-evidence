#!/usr/bin/env python3
"""Issue #7188: Three Visits lets you search entire deck for any card.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:confirmed, source:discord):
  "[[three visits]] lets you search entire deck for any card like a
   demonic tutor."

Oracle text (verified from pinned card-data.json, v0.85.0):
  "Search your library for a Forest card, put it onto the battlefield,
   then shuffle."

Parse (pinned card-data.json, v0.85.0): the Spell ability carries
  SearchLibrary{filter: Typed[Land, {Subtype: Forest}], count: Fixed 1,
  reveal: false} chained to ChangeZone(Library->Battlefield) chained to
  Shuffle. The parser claims the correct filter, so the defect (if any)
  is in the engine's runtime library-search candidate generation/filter
  evaluation, not the parse.

Expected behavior: only Forest cards are selectable; the chosen card
  enters the battlefield; the library is shuffled.

Reported behavior: the entire library is selectable, without the Forest
  filter (like Demonic Tutor).

Scenario: P0 casts Three Visits ({1}{G}) on the native engine with a
  60-card library containing Forest and non-Forest cards (Forests,
  Swamps, Llanowar Elves, Mountains). The driver captures the full
  advertised candidate set of the library-search opportunity and
  classifies every available candidate against the pinned card data
  (Forest subtype or not). Deterministic policy: answer the search with
  the first available Forest candidate.

Assertions:
  A1_setup_ok     pre.json: Three Visits in P0 hand, PreCombatMain,
                  life 20/20.
  A2_search_prompted  A library-search opportunity was advertised to P0
                  with a captured candidate list.
  A3_filter_applied   Every AVAILABLE advertised candidate is a Forest
                  card (per pinned card-data subtypes). Fails if any
                  available non-Forest candidate (e.g. Swamp, Llanowar
                  Elves, Mountain) is offered - the reported bug.
  A4_forest_enters    The submitted Forest is on P0's battlefield at post.
  A5_shuffle          Library count dropped by exactly 1 across the
                  search (one card moved to BF), stack empty, game
                  advanced past the cast turn.
  A6_cleanup          post.json: stack empty; turn advanced past cast
                  turn; no stuck waiting_for.

Verdict rule:
  blocked        iff A1 fails (setup never reached) or the search
                 opportunity never appears (can't exercise the claim).
  reproduced     iff A1+A2 pass and A3 fails (non-Forest cards
                 selectable) - or A4 fails after an accepted answer.
  not-reproduced iff A1..A6 all pass.

Evidence: evidence/7188/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7188.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts).
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7188
RUN_ID = os.environ.get("RUN_ID", "20260916-7188")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

VISITS = "three visits"
FOREST = "forest"
SWAMP = "swamp"
ELVES = "llanowar elves"
MOUNTAIN = "mountain"
PLAINS = "plains"

P0_DECK = [(VISITS, 12), (FOREST, 12), (SWAMP, 12), (ELVES, 12),
           (MOUNTAIN, 12)]
P1_DECK = [(PLAINS, 60)]

RELDIR = f"{BACKFILL}/server/releases/v0.85.0"
CARDDATA = json.load(open(f"{RELDIR}/data/card-data.json"))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "single-user",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode at pin time "
                      "2026-09-16; binary digest also matched GitHub asset "
                      "digest; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run "
                      "(AGENTS.md: never copy SERVER_IDENTITY hashes); "
                      "release v0.85.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-16",
    "observed_at": "2026-09-16",
    "handshake": "ServerHello observed pre-run on 127.0.0.1:9374: "
                 "v0.85.0 / cb58ef5 / protocol 72",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/20260916-7188/ (prior v0.84.0 server "
              "replaced after the release advanced)",
}


def is_forest_card(name):
    """True iff the pinned card data gives the card the Forest subtype."""
    c = CARDDATA.get((name or "").strip().lower())
    if not c:
        return None
    ct = c.get("card_type") or {}
    subs = [str(s).lower() for s in (ct.get("subtypes") or [])]
    return "forest" in subs


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    try:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def library_count(state, pid):
    p = player_of(state, pid)
    for k in ("library", "deck", "library_count"):
        v = p.get(k)
        if isinstance(v, list):
            return len(v)
        if isinstance(v, int):
            return v
    return None


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


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


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def stack_entries(state):
    return state.get("stack") or []


def drain_rejections(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            out.append({"type": t, "data": data})
    return out


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


def choice_status(ch):
    st = ch.get("status")
    if isinstance(st, dict):
        return str(st.get("type") or st.get("status") or "unknown")
    return str(st) if st else "unknown"

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> cast -> search_seen -> observed -> done
        "game_code": None,
        "visits_cast_turn": None,
        "visits_spell_oid": None,
        "lib_count_at_search": None,
        "chosen_name": None,
        "chosen_oid_hint": None,
        "forest_on_bf_turn": None,
        "search_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "last_rev_seen": {},
        "last_rev_change": {},
        "need_post_export": False,
        "post_exported": False,
        "mull_count": {0: 0, 1: 0},
        "observed_at": None,
        "search_answer_rejected": False,
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "search_candidates": [], "search_shapes": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-visits")
    p1 = PhaseClient("P1-idle")
    await p0.connect()
    await p1.connect()
    say("both clients connected (protocol 72)")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or (sess or {}).get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            ST["exports"][name] = True
            say(f"exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def mulligan_pending_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = (p.get("phase") or {})
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan_decide(c, pid, tag, st, state, want):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        rev = st.get("state_revision", -1)
        if acted(f"mull{pid}", rev):
            return True
        if want is not None and want not in hand_lnames(state, pid):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            ST["mull_count"][pid] += 1
            say(f"[{tag}] mulligan #{ST['mull_count'][pid]} (no {want})")
        else:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        if wf_player(state) != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["search_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if any(k in tx for k in protect):
                    return 2
                if MOUNTAIN in tx or SWAMP in tx:
                    return 0
                if ELVES in tx:
                    return 1
                return 3
            pick = sorted(chs, key=rank)[0]
            ST["search_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                {"interactionId": iid,
                 "response": {"type": "choose",
                              "data": {"choiceId": pick.get("id")}}}
            )
            return True
        return False

    def cast_spell_action(acts, state, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def untapped_lands(state, pid):
        out = []
        for oid, o in (state.get("objects", {}) or {}).items():
            if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                    and lname(state, int(oid)) in (FOREST, SWAMP, MOUNTAIN,
                                                   PLAINS):
                if not o.get("tapped"):
                    out.append(int(oid))
        return out

    async def scan_search(st, state):
        """Detect + capture the Three Visits library-search opportunity.
        Returns True if the driver answered it this tick."""
        vi = get_vi(st)
        if not vi:
            return False
        answered = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            full = json.dumps(opp, default=str)
            low = full.lower()
            is_search = ("search" in low or "library" in low) and len(chs) > 4
            if not is_search:
                continue
            shape_key = (rtype, len(chs))
            if shape_key not in obs["search_shapes"]:
                obs["search_shapes"].append(shape_key)
            if iid in ST["search_iids"]:
                continue
            # --- capture the full candidate set ---
            cands = []
            for ch in chs:
                nm = choice_text(ch)
                cands.append({"name": nm, "id": ch.get("id"),
                              "status": choice_status(ch),
                              "is_forest": is_forest_card(nm)})
            obs["search_candidates"] = cands
            wire("search_opportunity",
                 {"iid": iid, "rtype": rtype, "n": len(cands),
                  "candidates": cands})
            avail = [c for c in cands if c["status"] == "available"]
            non_forest_avail = [c for c in avail if c["is_forest"] is False]
            unknown = [c for c in avail if c["is_forest"] is None]
            say(f"[P0] SEARCH seen: iid={iid} rtype={rtype} "
                f"total={len(cands)} available={len(avail)} "
                f"forest_avail={sum(1 for c in avail if c['is_forest'])} "
                f"NONFOREST_avail={len(non_forest_avail)} unknown={len(unknown)}")
            if non_forest_avail:
                say(f"[P0] non-Forest available candidates: "
                    f"{[c['name'] for c in non_forest_avail][:12]}")
            if "mid" not in ST["exports"]:
                ST["lib_count_at_search"] = library_count(state, 0)
                await export_named("mid")
            # --- deterministic policy: pick first available Forest ---
            forests = [c for c in avail if c["is_forest"]]
            pick = forests[0] if forests else None
            if not pick:
                say("[P0] WARNING: no available Forest candidate to answer "
                    "with; leaving the prompt open for observation")
                ST["search_iids"].append(iid)  # don't re-capture
                continue
            spec = data.get("spec") or {}
            sub_type = spec.get("type") if isinstance(spec, dict) else None
            if rtype == "exactChoices":
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick["id"]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": sub_type or "sequence",
                                    "data": {"choiceIds": [pick["id"]]}}}
            ST["chosen_name"] = pick["name"]
            say(f"[P0] answering search with Forest: {pick['name']}")
            wire("search_submission", {"submission": sub})
            await p0.send_interaction(sub)
            ST["search_iids"].append(iid)
            ST["stage"] = "search_seen"
            ST["search_answers"] = ST.get("search_answers", 0) + 1
            answered = True
        return answered

    def observe_bf_forest(state):
        if ST.get("forest_on_bf_turn") is not None:
            return
        for oid in bf_ids(state, 0):
            if lname(state, oid) == FOREST and ST.get("chosen_name"):
                ST["forest_on_bf_turn"] = state.get("turn_number")
                say(f"[obs] Forest on P0 BF oid={oid} "
                    f"(turn {ST['forest_on_bf_turn']})")
                wire("forest_on_bf", {"oid": oid,
                                      "turn": ST["forest_on_bf_turn"]})
                break

    async def combat_empty(c, pid, tag, st, state, acts):
        rev = st.get("state_revision", -1)
        wtype = wf_of(state).get("type") or ""
        if wtype == "DeclareAttackers" and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_atk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await submit_as_is(c, {"type": wtype, "data": d})
                return True
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_decide(p0, 0, "P0", st, state, VISITS):
            return
        if await handle_discard(p0, 0, "P0", st, state, (VISITS,)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        for rj in drain_rejections(p0):
            obs["rejections"].append({"stage": ST["stage"], **rj})
            say(f"[P0] REJECTION in stage {ST['stage']}: "
                f"{json.dumps(rj, default=str)[:300]}")
            # #4509: a rejected iid must not stay answered - but cap
            # re-answers so a hard-rejecting engine can't spin the driver
            # forever (the stall itself is evidence)
            blob = json.dumps(rj, default=str)
            for iid in list(ST["search_iids"]):
                if str(iid) in blob:
                    ST["search_answers_failed"] = \
                        ST.get("search_answers_failed", 0) + 1
                    if ST["search_answers_failed"] > 5:
                        say(f"[P0] search answer rejected >5x; "
                            f"leaving iid {iid} open as stall evidence")
                        ST["stage"] = "search_stalled"
                    else:
                        ST["search_iids"].remove(iid)
                        say(f"[P0] un-answered rejected iid {iid}")
            if ST["stage"] == "search_seen":
                ST["search_answer_rejected"] = True
        observe_bf_forest(state)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        # the search prompt may arrive outside priority waits; check always
        if await scan_search(st, state):
            return

        if ST["forest_on_bf_turn"] is not None \
                and ST["stage"] in ("search_seen", "cast"):
            ST["stage"] = "observed"
            ST["observed_at"] = time.time()
            say("[P0] stage -> observed (submitted Forest on BF)")

        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # ---- main-phase actions ----
        if my_priority(state, 0) \
                and phase in ("PreCombatMain", "PostCombatMain") \
                and not stack_entries(state):
            # land drop: forests first (need green)
            for oid in hand_ids(state, 0):
                nm = lname(state, oid)
                if nm in (FOREST, SWAMP, MOUNTAIN):
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            # export pre once: Visits in hand and {1}{G} mana ready
            unt = untapped_lands(state, 0)
            green = any(lname(state, o) == FOREST for o in unt)
            if ST["stage"] == "setup" and VISITS in hand_lnames(state, 0) \
                    and len(unt) >= 2 and green \
                    and "pre" not in ST["exports"]:
                await export_named("pre")
            # cast Three Visits
            if ST["stage"] == "setup" and VISITS in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, VISITS)
                if ca and not acted("visitscast", rev):
                    ST["visits_spell_oid"] = (ca.get("data") or {}).get(
                        "object_id")
                    ST["visits_cast_turn"] = turn
                    say(f"[P0] casting Three Visits "
                        f"oid={ST['visits_spell_oid']} (turn {turn})")
                    await submit_as_is(p0, ca)
                    wire("visits_cast",
                         {"oid": ST["visits_spell_oid"], "turn": turn})
                    ST["stage"] = "cast"
                    return

        # after the observation: let the game advance a bit, then export
        # post and finish
        if ST["stage"] == "observed" and my_priority(state, 0) \
                and not stack_entries(state):
            cast_turn = ST.get("visits_cast_turn") or 0
            if turn > cast_turn or (
                    ST.get("observed_at")
                    and time.time() - ST["observed_at"] > 45):
                ST["need_post_export"] = True
                ST["stage"] = "done"
                say("[P0] observation window closed; stage -> done")
                return

        # default: pass priority (gated on my_priority, cf. #4509)
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_decide(p1, 1, "P1", st, state, None):
            return
        if await handle_discard(p1, 1, "P1", st, state, ()):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        for rj in drain_rejections(p1):
            obs["rejections"].append({"stage": "p1", **rj})
        observe_bf_forest(state)
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == PLAINS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST.get("need_post_export") and not ST.get("post_exported"):
                await export_named("post")
                ST["post_exported"] = True
                ST["need_post_export"] = False
            if ST["stage"] == "done":
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            rev = st.get("state_revision", -1)
            prev = ST["last_rev_seen"].get(tag)
            ST["last_rev_seen"][tag] = rev
            if rev != prev:
                ST["last_rev_change"][tag] = time.time()
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 900:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                for tag in ("P0", "P1"):
                    rev = ST["last_rev_seen"].get(tag)
                    chg = ST["last_rev_change"].get(tag, t0)
                    if time.time() - chg > 45:
                        say(f"[watchdog] {tag} revision {rev} stale "
                            f"{int(time.time()-chg)}s: turn="
                            f"{s.get('turn_number')} phase="
                            f"{s.get('phase')} wf="
                            f"{(s.get('waiting_for') or {}).get('type')} "
                            f"pp={s.get('priority_player')}")
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"chosen={ST['chosen_name']} "
                    f"forest_on_bf={ST['forest_on_bf_turn']}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if "post" not in ST.get("exports", {}):
            await export_named("post")
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre = states.get("pre", {})
        mid = states.get("mid", {})
        post = states.get("post", {})

        def life_of(state, pid):
            return player_of(state, pid).get("life")

        # ---- A1: setup ----
        a1 = ("pre" in states
              and VISITS in [str(x).lower() for x in
                             [obj_name(pre, o) for o in hand_ids(pre, 0)]]
              and pre.get("phase") in ("PreCombatMain", "PostCombatMain")
              and life_of(pre, 0) == 20 and life_of(pre, 1) == 20)
        notes.append(
            f"A1: pre_exported={'pre' in states} visits_in_hand="
            f"{VISITS in [str(x).lower() for x in [obj_name(pre, o) for o in hand_ids(pre, 0)]] if pre else None} "
            f"phase={pre.get('phase') if pre else None} "
            f"life={[life_of(pre, 0), life_of(pre, 1)] if pre else None}")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: search prompted ----
        cands = obs.get("search_candidates") or []
        a2 = bool(a1 and cands)
        notes.append(
            f"A2: search opportunity advertised={bool(cands)} "
            f"candidates={len(cands)} "
            f"shapes={obs.get('search_shapes')}")
        ass["A2_search_prompted"] = "passed" if a2 else "failed"

        # ---- A3: filter applied ----
        avail = [c for c in cands if c.get("status") == "available"]
        non_forest = [c for c in avail if c.get("is_forest") is False]
        unknown = [c for c in avail if c.get("is_forest") is None]
        a3 = bool(a2 and avail and not non_forest and not unknown)
        notes.append(
            f"A3: available={len(avail)} forest_avail="
            f"{sum(1 for c in avail if c.get('is_forest'))} "
            f"non_forest_avail={len(non_forest)} unknown_avail={len(unknown)}"
            + (f" non_forest_names={[c['name'] for c in non_forest][:15]}"
               if non_forest else "")
            + (f" unknown_names={[c['name'] for c in unknown][:10]}"
               if unknown else ""))
        ass["A3_filter_applied"] = "passed" if a3 else "failed"

        # ---- A4: chosen Forest entered the battlefield ----
        chosen = ST.get("chosen_name")
        forest_bf = [oid for oid in bf_ids(post, 0)
                     if lname(post, oid) == FOREST] if post else []
        a4 = bool(a3 and chosen and forest_bf
                  and not ST.get("search_answer_rejected"))
        notes.append(
            f"A4: chosen={chosen} forest_on_p0_bf_at_post={forest_bf} "
            f"search_answer_rejected={ST.get('search_answer_rejected')}")
        ass["A4_forest_enters"] = "passed" if a4 else "failed"

        # ---- A5: shuffle (library -1, game advanced) ----
        lib_mid = library_count(mid, 0) if mid else None
        lib_post = library_count(post, 0) if post else None
        lib_delta = (lib_mid - lib_post) if (lib_mid is not None
                                             and lib_post is not None) else None
        a5 = bool(a4 and lib_delta == 1 and not stack_entries(post)
                  and (post.get("turn_number") or 0)
                  > (ST.get("visits_cast_turn") or 0))
        notes.append(
            f"A5: lib_at_search={ST.get('lib_count_at_search')} "
            f"lib_mid={lib_mid} lib_post={lib_post} delta={lib_delta} "
            f"(expect 1) stack_empty={not stack_entries(post) if post else None} "
            f"turn_post={post.get('turn_number') if post else None} "
            f"cast_turn={ST.get('visits_cast_turn')}")
        ass["A5_shuffle"] = "passed" if a5 else "failed"

        # ---- A6: cleanup ----
        wf_post = (post.get("waiting_for") or {}).get("type") if post else None
        a6 = bool(post and not stack_entries(post)
                  and (post.get("turn_number") or 0)
                  > (ST.get("visits_cast_turn") or 0)
                  and wf_post in ("Priority", None))
        notes.append(
            f"A6: post present={bool(post)} stack_empty="
            f"{not stack_entries(post) if post else None} "
            f"wf_post={wf_post} turns_seen={sorted(ST['turns_seen'])}")
        ass["A6_cleanup"] = "passed" if a6 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed" \
                or ass["A2_search_prompted"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup or search prompt never "
                         "reached - the reported claim could not be "
                         "exercised")
        elif ass["A3_filter_applied"] != "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: non-Forest cards were "
                         "advertised as available search candidates - "
                         "the reported 'like a demonic tutor' behavior")
        elif ass["A4_forest_enters"] != "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the answered Forest did not "
                         "reach the battlefield")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_search_prompted", "A3_filter_applied",
                  "A4_forest_enters", "A5_shuffle", "A6_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: only Forest cards "
                         "selectable; chosen Forest entered; library "
                         "shuffled (count -1)")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-16",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 72,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {
                "stage": ST["stage"],
                "visits_cast_turn": ST["visits_cast_turn"],
                "visits_spell_oid": ST["visits_spell_oid"],
                "chosen_name": ST["chosen_name"],
                "lib_count_at_search": ST["lib_count_at_search"],
                "forest_on_bf_turn": ST["forest_on_bf_turn"],
                "search_answer_rejected": ST["search_answer_rejected"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
                "mull_count": ST["mull_count"],
            },
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is a passive second seat (lands only, no attacks) - "
                "opponent interaction is not exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Shuffle verified by library-count delta (-1) and game "
                "progression, not by order randomness (library order is "
                "not asserted).",
                "Candidate Forest classification uses the pinned "
                "card-data.json subtypes; a candidate whose name is not "
                "in card data would be counted as unknown, not Forest.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        say(f"server run dir: runs/{srv_run}")
        try:
            with open(f"{BACKFILL}/runs/{srv_run}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            gc = ST.get("game_code") or ""
            excerpt = [ln for ln in clean.splitlines()
                       if gc and gc in ln]
            if not excerpt:
                excerpt = clean.splitlines()[-400:]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        # close logs BEFORE hashing the manifest (#7176 lesson)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1220
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7188 - Three Visits library search",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.85.0 (cb58ef5) protocol 72 - 2026-09-16"
               " - SearchLibrary Typed[Land, {Subtype: Forest}]",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: '[[three visits]] lets you search entire deck",
                "for any card like a demonic tutor.' Oracle: 'Search your",
                "library for a Forest card, put it onto the battlefield,",
                "then shuffle.' Parse (pinned card-data): SearchLibrary",
                "filter Typed[Land, {Subtype: Forest}], count 1 ->",
                "ChangeZone Library->Battlefield -> Shuffle. The parser",
                "claims the correct filter; runtime candidates are the",
                "question."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Three Visits in P0 hand; PreCombatMain; 20/20",
            "A2_search_prompted": "library-search opportunity advertised",
            "A3_filter_applied": "every AVAILABLE candidate is a Forest",
            "A4_forest_enters": "submitted Forest on P0 BF at post",
            "A5_shuffle": "library count -1; stack empty; game advanced",
            "A6_cleanup": "no stuck waiting_for; turn advanced",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
                   "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
            d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run["driver_state"]
        cands = run["observations"].get("search_candidates") or []
        avail = [c for c in cands if c.get("status") == "available"]
        nfa = [c["name"] for c in avail if c.get("is_forest") is False][:8]
        for ln in [
                f"visits cast turn={ds.get('visits_cast_turn')} "
                f"chosen={ds.get('chosen_name')} "
                f"forest_on_bf_turn={ds.get('forest_on_bf_turn')}",
                f"candidates advertised: total={len(cands)} "
                f"available={len(avail)}",
                f"non-Forest available: "
                f"{', '.join(nfa) if nfa else '(none)'}",
                f"lib at search={ds.get('lib_count_at_search')} "
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:14])}",
                "Exports: pre.json (Visits in hand, mana ready),",
                "mid.json (search prompt pending, candidates captured),",
                "post.json (Forest on BF, stack empty, later turn).",
        ]:
            d.text((24, y), ln[:108], fill=(160, 175, 195))
            y += 22
        d.text((24, y + 14), "Generated from saved states/assertions; not "
               "a gameplay screenshot.", fill=(110, 125, 145))
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        import hashlib as _hl
        files = sorted(
            f for f in os.listdir(EVDIR)
            if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = _hl.sha256()
            with open(f"{EVDIR}/{fn}", "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
