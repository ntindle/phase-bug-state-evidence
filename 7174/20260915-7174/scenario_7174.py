#!/usr/bin/env python3
"""phase-rs/phase #7174 - Landfall triggering 2x on Bristly Bill, Spine Sower
when cracking a fetch land.

Oracle (pinned v0.83.0 card-data):
  Bristly Bill, Spine Sower {1}{G} 2/2 Legendary Creature - Plant Druid
  "Landfall - Whenever a land you control enters, put a +1/+1 counter on
   target creature."
  Wooded Foothills: "{T}, Pay 1 life, Sacrifice this land: Search your
   library for a Mountain or Forest card, put it onto the battlefield,
   then shuffle."

Reported: Bill in play; play a fetch land -> Bill triggers once (OK).
Crack the fetch land -> Bill triggers another 2x times (BUG).

Contract:
  A1_parse        - pinned card-data carries exactly one landfall trigger on
                    Bill: ChangesZone -> Battlefield, valid_card Land/You
                    [parse evidence]
  A2_setup_ok     - Bill on P0 BF; control Foothills played and its trigger
                    resolved (counters 0->1); pre.json exported before crack
  A3_control_play - playing the fetch land from hand created exactly ONE
                    Bill trigger (counter delta across the play event == 1)
  A4_fetch        - cracking the fetch (fetched Forest entering) creates
                    exactly ONE Bill trigger (counter delta across the fetch
                    event == 1). FAILED = the reported bug (delta == 2)
  A5_cleanup      - stack empty, game advanced; post.json exported

Verdict rule: reproduced iff A1-A3 pass and the fetch counter delta == 2
(the reported double-trigger). not-reproduced iff A1-A4 pass (delta == 1).
blocked otherwise (including an unexpected delta).
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7174
RUN_ID = os.environ.get("RUN_ID", "20260915-7174")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BILL = "bristly bill, spine sower"
FOOTHILLS = "wooded foothills"
FOREST = "forest"

P0_DECK = [(BILL, 4), (FOOTHILLS, 8), (FOREST, 48)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": ("pinned v0.83.0 (minisign-verified binary + signed data "
               "manifest, ledger server pin 2026-09-15); reusing the live "
               "v0.83.0 server on 127.0.0.1:9374 started for the 7173 run "
               "(run dir discovered live from the server process); "
               "ServerHello re-checked by this run."),
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def oname(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def bf_named(state, pid, name):
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and oname(o) == name):
            return int(oid)
    return None


def counters_of(state, oid):
    o = objects(state).get(str(oid)) or {}
    c = o.get("counters") or {}
    return int(c.get("P1P1", 0))


def hand_oids(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_lnames(state, pid):
    return [oname(o) for o in objects(state).values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def untapped_lands(state, pid):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and oname(o) in (FOREST, FOOTHILLS) and not o.get("tapped"))


def lib_count(state, pid, name):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Library" and o.get("controller") == pid
               and oname(o) == name)


def wf_of(state):
    return (state.get("waiting_for") or {})


def wf_type(state):
    return (wf_of(state).get("type") or "")


def wf_data(state):
    return wf_of(state).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def my_priority(state, pid):
    return wf_type(state) == "Priority" and wf_player(state) == pid


def is_my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid)


def stack_entries(state):
    return state.get("stack") or []


def bill_trigger_entries(state, bill_oid):
    """Bill's TriggeredAbility entries currently on the stack."""
    out = []
    for e in stack_entries(state):
        kind = e.get("kind") or {}
        if kind.get("type") != "TriggeredAbility":
            continue
        if e.get("source_id") != bill_oid:
            continue
        ability = ((kind.get("data") or {}).get("ability")) or {}
        desc = ability.get("description") or ""
        if "land you control enters" in desc.lower():
            out.append(e)
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg.get("type"),
                           "stage": ST.get("stage")})
    await c.send_action(msg)


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


# ------------------------------------------------------- viewer_interaction
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
                return str(v).lower()
    r = ref_of(ch)
    if r is not None:
        o = (state.get("objects") or {}).get(str(r))
        if o:
            return oname(o)
    return choice_text(ch).lower()


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


def server_run_dir():
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "phase-server" in line and "--games-db" in line:
                parts = line.split()
                i = parts.index("--games-db")
                db = parts[i + 1]
                return db.split("/runs/")[1].split("/")[0]
    except Exception as e:
        say("server_run_dir lookup failed:", e)
    return None


async def check_server_hello():
    import websockets
    try:
        async with websockets.connect("ws://127.0.0.1:9374/ws",
                                      max_size=10_000_000) as ws:
            raw = await asyncio.wait_for(ws.recv(), 5)
            msg = json.loads(raw)
            d = msg.get("data", {}) or {}
            say("ServerHello:", {k: d.get(k) for k in
                                 ("server_version", "build_commit",
                                  "protocol_version", "mode")})
            return d
    except Exception as e:
        say("ServerHello check failed:", e)
        return {}


ST = {"stage": "SETUP", "bill_oid": None, "foothills_oid": None,
      "bill_cast": False, "bill_cast_turn": None,
      "control_played": False, "control_resolved": False,
      "fetch_activated": False, "search_answered": False,
      "c_before_control": None, "c_after_control": None,
      "c_before_fetch": None, "c_after_fetch": None,
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "trigger_sightings": [], "rejections": [], "done": False,
      "last_target_oid": None}
ACTED = {}


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup_ok", "A3_control_play", "A4_fetch",
            "A5_cleanup")}

    hello = await check_server_hello()
    if hello:
        for k in ("server_version", "build_commit", "protocol_version"):
            if hello.get(k):
                SERVER_IDENTITY[k] = hello[k]

    # ---- A1: parse check against the pinned v0.83.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/card-data.json"))
        bill = cd[BILL]
        fh = cd[FOOTHILLS]
        say("bill oracle:", bill["oracle_text"].replace("\n", " | "))
        say("foothills oracle:", fh["oracle_text"].replace("\n", " | "))
        with open(f"{EVDIR}/parse_bill.json", "w") as f:
            json.dump({"oracle_text": bill["oracle_text"],
                       "triggers": bill.get("triggers"),
                       "abilities": bill.get("abilities")}, f, indent=1)
        with open(f"{EVDIR}/parse_foothills.json", "w") as f:
            json.dump({"oracle_text": fh["oracle_text"],
                       "abilities": fh.get("abilities")}, f, indent=1)
        trigs = bill.get("triggers") or []
        ok = (len(trigs) == 1
              and trigs[0].get("mode") == "ChangesZone"
              and trigs[0].get("destination") == "Battlefield"
              and (trigs[0].get("valid_card") or {}).get("type") == "Typed"
              and "Land" in ((trigs[0].get("valid_card") or {})
                             .get("type_filters") or [])
              and (trigs[0].get("valid_card") or {}).get("controller") == "You"
              and "land you control enters" in
              str(trigs[0].get("description") or "").lower())
        fh_abs = fh.get("abilities") or []
        fh_ok = any((a.get("effect") or {}).get("type") == "SearchLibrary"
                    for a in fh_abs)
        if ok and fh_ok:
            ass["A1_parse"] = "passed"
            notes.append("A1_parse: passed (Bill carries exactly one "
                         "landfall trigger: ChangesZone->Battlefield, "
                         "valid_card Land/You; Foothills carries the "
                         "SearchLibrary fetch ability)")
        else:
            ass["A1_parse"] = "failed"
            notes.append(f"A1_parse: FAILED - bill triggers={len(trigs)} "
                         f"foothills SearchLibrary present={fh_ok}")
        say(notes[-1])
    except Exception as ex:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse failed: {ex}")
        say(notes[-1])

    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            wire(f"export_{tag}", {"ok": True})
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def do_mulligan(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        names = hand_lnames(state, pid)
        lands = sum(1 for n in names if n in (FOREST, FOOTHILLS))
        if pid == 0:
            want_mull = (BILL not in names or lands < 2)
        else:
            want_mull = lands < 2
        if not want_mull or ST.get(f"mull_{tag}"):
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] keeps (lands={lands}, bill={BILL in names})")
        else:
            ST[f"mull_{tag}"] = True
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{tag}] mulligans (lands={lands}, bill={BILL in names})")

    def pick_discard(state, pid, n):
        # protect Bill and Foothills; discard Forests first
        def rank(oid):
            nm = oname(objects(state).get(str(oid), {}))
            if nm == BILL:
                return 3
            if nm == FOOTHILLS:
                return 2
            return 0 if nm == FOREST else 1
        return [int(x) for x in sorted(hand_oids(state, pid), key=rank)[:n]]

    async def handle_bill_target(c, st, state, tag):
        """Answer Bill's landfall TargetSelection, targeting Bill itself.
        Gated on a Bill TriggeredAbility actually being on the stack."""
        if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
            return False
        boid = ST["bill_oid"] or bf_named(state, 0, BILL)
        if boid is None:
            return False
        if not bill_trigger_entries(state, boid):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            chs, _rtype = vi_choices(opp)
            want = None
            for ch in chs:
                r = ref_of(ch)
                if (r is not None and int(r) == int(boid)) or \
                        choice_card_name(ch, state) == BILL:
                    want = ch
                    break
            if want is None:
                continue
            ST["last_target_oid"] = ref_of(want)
            say(f"[{tag}] Bill trigger targets Bill (oid {boid})")
            await answer_vi(c, opp, want, tag)
            return True
        return False

    async def handle_search(c, st, state, tag):
        """Answer the fetch's library search by picking a Forest."""
        wt = (wf_type(state) or "").lower()
        if not ("search" in wt or "library" in wt):
            return False
        if wf_player(state) != 0:
            return False
        if ST["search_answered"]:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            chs, _rtype = vi_choices(opp)
            if not chs:
                continue
            want = None
            for ch in chs:
                if choice_card_name(ch, state) == FOREST:
                    want = ch
                    break
            if want is None:
                say(f"[{tag}] WARNING: Forest not among search candidates; "
                    f"first 5: {[choice_card_name(ch, state) for ch in chs[:5]]}")
                continue
            ST["search_choice"] = choice_text(want)[:120]
            say(f"[{tag}] search pick: {ST['search_choice']}")
            await answer_vi(c, opp, want, tag)
            ST["search_answered"] = True
            ST["stage"] = "FETCH_TRIGGERS"
            return True
        return False

    async def seat_tick(c, pid, tag):
        drain_rejections(c)
        st = c.latest or {}
        state = st.get("state", st)
        wf = wf_of(state)
        wtype = wf_type(state)
        acts = merged_actions(st)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        rev = st.get("state_revision", -1)

        # legend-choice defensive handling (submit advertised as-is)
        for a in acts:
            if "Legend" in (a.get("type") or ""):
                if not acted(f"leg{pid}", rev):
                    await submit_as_is(c, a)
                    say(f"[{tag}] legend-choice submitted as-is")
                return
        # mulligan (+ bottom-cards SelectCards after a mulligan)
        if wtype == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            mine = [p for p in pend if p.get("player") == pid]
            if mine and (mine[0].get("phase") or {}).get("type") \
                    == "BottomCards":
                n = (mine[0].get("phase") or {}).get("count") or 1
                picks = pick_discard(state, pid, n)
                if picks and not acted(f"bott{pid}", rev):
                    await submit_as_is(
                        c, {"type": "SelectCards",
                            "data": {"cards": picks}})
                    say(f"[{tag}] bottoms {len(picks)} after mulligan")
                return
            if find_action(acts, "MulliganDecision"):
                await do_mulligan(c, pid, tag)
            return
        # hand-size discard
        if wtype == "DiscardToHandSize" and wf_player(state) == pid:
            n = wf_data(state).get("count") or max(
                0, len(hand_oids(state, pid)) - 7)
            picks = pick_discard(state, pid, n)
            if picks and not acted(f"hsd{pid}", rev):
                await submit_as_is(
                    c, {"type": "SelectCards", "data": {"cards": picks}})
                say(f"[{tag}] discards {len(picks)} to hand size")
            return
        # combat: never attack or block
        if wtype == "DeclareAttackers" and wf_player(state) == pid:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub.setdefault("data", {})["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return
        if wtype == "DeclareBlockers" and wf_player(state) == pid:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(db)
                sub.setdefault("data", {})["assignments"] = []
                await submit_as_is(c, sub)
            return
        # P0's prompt handlers run BEFORE the no-pass guard: the guard's job
        # is to stop the default PassPriority fallthrough, not to swallow
        # the handlers for Bill's TargetSelection or the fetch search.
        if pid == 0:
            # Bill trigger targeting (both control and fetch events)
            if ST["stage"] in ("CONTROL_WAIT", "FETCH_TRIGGERS"):
                if await handle_bill_target(c, st, state, tag):
                    return
            # fetch search
            if ST["stage"] == "FETCH_SEARCH":
                if await handle_search(c, st, state, tag):
                    return
        # never pass priority while our own decision is pending
        if wtype in ("OptionalCostChoice", "TargetSelection", "ChooseXValue",
                     "EntryControllerChoice", "ChooseOneOfBranch",
                     "CombatTaxPayment", "CopyRetarget") \
                and wf_player(state) == pid:
            return
        # P1 is fully passive beyond lands/passes
        if pid == 1:
            if is_my_main(state, 1) and my_priority(state, 1):
                pl = find_action(acts, "PlayLand")
                if pl and not acted(f"land1_{turn}", rev):
                    await submit_as_is(c, pl)
                    return
            if my_priority(state, pid):
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(c, pp)
            return
        # ---- P0 ----
        if is_my_main(state, 0) and my_priority(state, 0):
            boid = bf_named(state, 0, BILL)
            if boid is not None:
                ST["bill_oid"] = boid
            # cast Bill (legendary: only if none on board)
            if (ST["stage"] == "SETUP" and not ST["bill_cast"]
                    and boid is None and untapped_lands(state, 0) >= 2):
                for oid in hand_oids(state, 0):
                    if oname(objects(state).get(str(oid), {})) != BILL:
                        continue
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if ("cast" in (a.get("type") or "").lower()
                                and str(d.get("object_id", "")) == str(oid)):
                            await submit_as_is(c, a)
                            ST["bill_cast"] = True
                            ST["bill_cast_turn"] = turn
                            say(f"[P0] cast Bill (turn {turn})")
                            return
            # control leg: play a Foothills from hand
            if ST["stage"] == "SETUP" and boid is not None:
                for oid in hand_oids(state, 0):
                    if oname(objects(state).get(str(oid), {})) != FOOTHILLS:
                        continue
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if (a.get("type") == "PlayLand"
                                and str(d.get("object_id", "")) == str(oid)):
                            ST["c_before_control"] = counters_of(state, boid)
                            ST["foothills_oid"] = oid
                            await submit_as_is(c, a)
                            ST["control_played"] = True
                            ST["stage"] = "CONTROL_WAIT"
                            say(f"[P0] plays Foothills (control, turn {turn}); "
                                f"bill counters before={ST['c_before_control']}")
                            return
            # fetch leg: activate the Foothills
            if ST["stage"] == "CONTROL_DONE" and boid is not None \
                    and not ST["fetch_activated"]:
                foid = bf_named(state, 0, FOOTHILLS)
                if foid is not None:
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if (a.get("type") == "ActivateAbility"
                                and d.get("source_id") == foid):
                            if not ST["pre_exported"]:
                                await export_named("pre")
                                ST["pre_exported"] = True
                            ST["c_before_fetch"] = counters_of(state, boid)
                            ST["fetch_turn"] = turn
                            wire("fetch_activate", {"turn": turn,
                                                    "foothills": foid})
                            await submit_as_is(c, a)
                            ST["fetch_activated"] = True
                            ST["stage"] = "FETCH_SEARCH"
                            say(f"[P0] activates Foothills (turn {turn}); "
                                f"bill counters before={ST['c_before_fetch']}")
                            return
            # generic land drop: in SETUP drop only Forests (keep a
            # Foothills in hand for the control leg); other stages don't
            # need land drops except CONTROL_DONE (harmless ramp)
            if ST["stage"] == "SETUP":
                for a in acts:
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id")
                    if (a.get("type") == "PlayLand" and oid is not None
                            and oname(objects(state).get(str(oid), {}))
                            == FOREST):
                        await submit_as_is(c, a)
                        return
            elif ST["stage"] == "CONTROL_DONE":
                pl = find_action(acts, "PlayLand")
                if pl:
                    await submit_as_is(c, pl)
                    return
        # default: pass priority
        if my_priority(state, pid):
            pp = find_action(acts, "PassPriority")
            if pp:
                await submit_as_is(c, pp)

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid, post = (states.get("pre"), states.get("mid"),
                          states.get("post"))

        boid = ST["bill_oid"]
        cbc = ST["c_before_control"]
        cac = ST["c_after_control"]
        cbf = ST["c_before_fetch"]
        caf = ST["c_after_fetch"]

        # ---- A2: setup ----
        if pre is not None and boid is not None:
            pb = bf_named(pre, 0, BILL)
            ok = pb is not None and ST["control_resolved"]
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: Bill on P0 BF in pre.json={pb is not None}, "
                         f"control trigger resolved={ST['control_resolved']}, "
                         f"counters(before control)={cbc}, "
                         f"(after control)={cac}, (before fetch)={cbf}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing or Bill never on BF")

        # ---- A3: control leg (playing the fetch from hand) ----
        if cac is not None and cbc is not None:
            d = cac - cbc
            ass["A3_control_play"] = "passed" if d == 1 else "failed"
            notes.append(f"A3: control play counter delta={d} "
                         f"(expected 1) -> "
                         f"{'passed' if d == 1 else 'FAILED'}")
        else:
            ass["A3_control_play"] = "failed"
            notes.append("A3 failed: control counter values missing")

        # ---- A4: the reported bug (cracking the fetch) ----
        fetch_delta = None
        if caf is not None and cbf is not None:
            fetch_delta = caf - cbf
            ass["A4_fetch"] = "passed" if fetch_delta == 1 else "failed"
            notes.append(f"A4: fetch counter delta={fetch_delta} "
                         f"(expected 1; 2 = the reported double-trigger) -> "
                         f"{'passed' if fetch_delta == 1 else 'FAILED'}")
        else:
            ass["A4_fetch"] = "failed"
            notes.append("A4 failed: fetch counter values missing")

        # ---- A5: cleanup ----
        if post is not None:
            empty = len(stack_entries(post)) == 0
            ass["A5_cleanup"] = "passed" if empty else "failed"
            notes.append(f"A5: post stack empty={empty}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: post.json missing")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if all(ass[k] == "passed" for k in
               ("A1_parse", "A2_setup_ok", "A3_control_play")):
            if ass["A4_fetch"] == "passed":
                verdict = "not-reproduced"
            elif fetch_delta == 2:
                verdict = "reproduced"
            else:
                verdict = "blocked"
                notes.append(f"verdict blocked: fetch delta={fetch_delta} "
                             "is neither the correct 1 nor the reported 2")
        else:
            verdict = "blocked"
        say("VERDICT:", verdict)

        srv_run = server_run_dir()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": ("Landfall triggering 2x on Bristly Bill, Spine Sower "
                      "for (only) cracking fetch land"),
            "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "scope": ("Bill landfall trigger count for (a) playing a fetch "
                      "land from hand [control] and (b) cracking the fetch "
                      "for a Forest [reported bug]; native engine, two "
                      "human-client seats, P1 fully passive"),
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "bill_oid": boid,
                "bill_cast_turn": ST["bill_cast_turn"],
                "c_before_control": cbc, "c_after_control": cac,
                "c_before_fetch": cbf, "c_after_fetch": caf,
                "control_played": ST["control_played"],
                "control_resolved": ST["control_resolved"],
                "fetch_activated": ST["fetch_activated"],
                "search_answered": ST["search_answered"],
                "search_choice": ST.get("search_choice"),
                "last_target_oid": ST["last_target_oid"],
                "trigger_sightings": [
                    {k: v for k, v in s.items()} for s in
                    ST["trigger_sightings"]],
                "rejections": ST["rejections"],
            },
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense playsets (4x Bill, 8x Foothills) are a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "P1 fully passive (lands, passes; no attacks or blocks).",
                "The reporter's UI-timing note (fetched-basic triggers "
                "surfacing only after Pass/combat with an untapped fetch "
                "target) is a display observation; this run counts "
                "engine-level trigger instances on the stack and final "
                "counter deltas.",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "server_run_note": ("reused the live v0.83.0 server on "
                                "127.0.0.1:9374 started for the 7173 run "
                                f"(games.db + server.log in runs/{srv_run}/); "
                                "ServerHello re-checked by this run; "
                                "server.log copied from the server's run dir."),
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7174.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{srv_run}/server.log",
                        f"{EVDIR}/server.log")
            say(f"copied server.log from runs/{srv_run}/")
        except Exception as e:
            notes.append(f"server.log copy failed: {e}")
            say("server.log copy failed:", e)
        render_png(run)
        files = ["pre.json", "mid.json", "post.json", "parse_bill.json",
                 "parse_foothills.json", "run.json", "scenario_7174.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # NOTE: no say() after the manifest is written - scenario_run.log is
        # part of the manifest, so further log appends would invalidate it.
        print("wrote manifest.sha256", flush=True)
        for fn in ("pre.json", "post.json", "parse_bill.json",
                   "parse_foothills.json", "run.json"):
            if os.path.exists(f"{EVDIR}/{fn}"):
                json.load(open(f"{EVDIR}/{fn}"))
        if os.path.exists(f"{EVDIR}/mid.json"):
            json.load(open(f"{EVDIR}/mid.json"))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        print("validation: all JSON parse, PNG readable, hashes match",
              flush=True)

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1060
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7174 - Bristly Bill, Spine Sower "
               "landfall triggers 2x", fill=(235, 240, 250))
        y += 24
        d.text((24, y), "when cracking a fetch land", fill=(235, 240, 250))
        y += 30
        d.text((24, y), "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-15 - "
               "2 seats", fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: Landfall - Whenever a land you control "
               "enters, put a +1/+1", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "counter on target creature. (Wooded Foothills: "
               "{T}, pay 1 life,", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "sacrifice: search library for Mountain/Forest, put "
               "onto battlefield.)", fill=(200, 210, 225))
        y += 30
        ds = run["driver_state"]
        d.text((24, y), f"control (play fetch from hand): counters "
               f"{ds['c_before_control']} -> {ds['c_after_control']} "
               f"(delta {ds['c_after_control'] - ds['c_before_control'] if ds['c_after_control'] is not None and ds['c_before_control'] is not None else '?'}, expected 1)",
               fill=(140, 160, 180))
        y += 26
        d.text((24, y), f"crack fetch (Forest enters): counters "
               f"{ds['c_before_fetch']} -> {ds['c_after_fetch']} "
               f"(delta {ds['c_after_fetch'] - ds['c_before_fetch'] if ds['c_after_fetch'] is not None and ds['c_before_fetch'] is not None else '?'}, expected 1; 2 = reported bug)",
               fill=(140, 160, 180))
        y += 26
        d.text((24, y), f"trigger sightings on stack: "
               f"{len(ds['trigger_sightings'])}",
               fill=(140, 160, 180))
        y += 32
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 24
        for k, v in run["assertions"].items():
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((36, y), f"{k}: {v}", fill=c)
            y += 22
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), n[:114], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1200
    last_tick = {0: 0, 1: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1):
                tag = ("P0", "P1")[pid]
                if time.time() - last_tick[pid] < 1.0:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            # cross-seat observation from P0's view
            st = p0.latest or {}
            state = st.get("state", st)
            turn = state.get("turn_number") or 0
            phase = state.get("phase") or ""
            boid = ST["bill_oid"] or bf_named(state, 0, BILL)
            if boid is not None:
                ST["bill_oid"] = boid
            # opportunistic stack sightings of Bill's landfall trigger
            if boid is not None and ST["stage"] in ("CONTROL_WAIT",
                                                    "FETCH_TRIGGERS"):
                for e in bill_trigger_entries(state, boid):
                    eid = e.get("id")
                    if not any(s["id"] == eid for s in
                               ST["trigger_sightings"]):
                        rec = {"id": eid, "turn": turn, "phase": phase,
                               "stage": ST["stage"]}
                        ST["trigger_sightings"].append(rec)
                        wire("bill_trigger_on_stack", rec)
                        say(f"TRIGGER SIGHTING: Bill landfall on stack "
                            f"(stage {ST['stage']}, turn {turn}, "
                            f"phase {phase})")
                        if ST["stage"] == "FETCH_TRIGGERS" \
                                and not ST["mid_exported"]:
                            await export_named("mid")
                            ST["mid_exported"] = True
            # control resolution: counters rose past c_before_control with
            # no Bill trigger left on the stack
            if ST["stage"] == "CONTROL_WAIT" and boid is not None \
                    and ST["c_before_control"] is not None:
                c = counters_of(state, boid)
                if c > ST["c_before_control"] \
                        and not bill_trigger_entries(state, boid):
                    ST["c_after_control"] = c
                    ST["control_resolved"] = True
                    ST["stage"] = "CONTROL_DONE"
                    say(f"control trigger resolved: counters "
                        f"{ST['c_before_control']} -> {c}")
                    wire("control_resolved",
                         {"before": ST["c_before_control"], "after": c,
                          "turn": turn, "phase": phase})
            # fetch resolution: counters rose past c_before_fetch with the
            # fetch fully resolved (Foothills sacrificed, no Bill trigger);
            # watchdog: if the fetch resolved with no counter change at all,
            # close the window after the state stays stable
            if ST["stage"] == "FETCH_TRIGGERS" and boid is not None \
                    and ST["c_before_fetch"] is not None:
                c = counters_of(state, boid)
                fh_gone = bf_named(state, 0, FOOTHILLS) is None
                no_trig = not bill_trigger_entries(state, boid)
                if c > ST["c_before_fetch"] and no_trig and fh_gone:
                    ST["c_after_fetch"] = c
                    ST["stage"] = "DONE"
                    ST["done"] = True
                    say(f"fetch triggers resolved: counters "
                        f"{ST['c_before_fetch']} -> {c}")
                    wire("fetch_resolved",
                         {"before": ST["c_before_fetch"], "after": c,
                          "turn": turn, "phase": phase,
                          "sightings": len(ST["trigger_sightings"])})
                elif fh_gone and no_trig and ST["search_answered"]:
                    ST["fetch_stable"] = ST.get("fetch_stable", 0) + 1
                    if ST["fetch_stable"] >= 30:
                        ST["c_after_fetch"] = c
                        ST["stage"] = "DONE"
                        ST["done"] = True
                        say(f"fetch window closed stable: counters "
                            f"{ST['c_before_fetch']} -> {c} "
                            f"(sightings={len(ST['trigger_sightings'])})")
                        wire("fetch_stable_close",
                             {"before": ST["c_before_fetch"], "after": c,
                              "turn": turn, "phase": phase,
                              "sightings": len(ST["trigger_sightings"])})
                else:
                    ST["fetch_stable"] = 0
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
            say("deadline hit")
    finally:
        await finish()
        for c, _ in (p0, 0), (p1, 1):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
