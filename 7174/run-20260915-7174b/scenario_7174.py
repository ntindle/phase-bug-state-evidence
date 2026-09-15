#!/usr/bin/env python3
"""phase-rs/phase #7174 - Landfall triggering 2x on Bristly Bill, Spine Sower
when cracking a fetch land.

Oracle (pinned v0.84.0 card-data):
  Bristly Bill, Spine Sower {1}{G} 2/2 Legendary Creature - Plant Druid
  "Landfall - Whenever a land you control enters, put a +1/+1 counter on
   target creature."
  Wooded Foothills: "{T}, Pay 1 life, Sacrifice this land: Search your
   library for a Mountain or Forest card, put it onto the battlefield,
   then shuffle."

Reported: Bill in play; play a fetch land -> Bill triggers once (OK).
Crack the fetch land -> Bill triggers another 2x times (BUG). With a
Zagoth Triome fetched the 2x triggers appeared immediately; with a basic
Forest fetched they surfaced only after clicking Pass / To Begin Combat.

Contract (two independent legs per event type, counter deltas decisive):
  A1_parse        - pinned card-data carries exactly one landfall trigger on
                    Bill: ChangesZone -> Battlefield, valid_card Land/You
  A2_setup_ok     - Bill on P0 BF; both Foothills played and their triggers
                    resolved; pre.json exported before the first crack
  A3_ctrl1        - playing Foothills#1 from hand created exactly ONE Bill
                    trigger (counter delta == 1)
  A3b_ctrl2       - playing Foothills#2 from hand created exactly ONE Bill
                    trigger (counter delta == 1)
  A4_triome       - cracking Foothills#1 (Zagoth Triome entering, the
                    reporter's primary case) creates exactly ONE Bill
                    trigger (counter delta == 1). FAILED = delta == 2,
                    the reported double-trigger.
  A5_forest       - cracking Foothills#2 (Forest entering) creates exactly
                    ONE Bill trigger (counter delta == 1)
  A6_cleanup      - stack empty, game advanced; post.json exported

Verdict rule: reproduced iff A1/A2/A3/A3b pass and any fetch counter
delta == 2 (the reported double-trigger). not-reproduced iff all
assertions pass (both fetch deltas == 1). blocked otherwise (including
an unexpected delta).
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
RUN_ID = os.environ.get("RUN_ID", "run-20260915-7174b")
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
TRIOME = "zagoth triome"

# 12x Foothills: the engine allows >4-of in custom games and the driver
# must draw a second fetch naturally after the first control leg; 8x
# once stranded a run 25 turns without a draw.
P0_DECK = [(BILL, 4), (FOOTHILLS, 12), (TRIOME, 8), (FOREST, 36)]
P1_DECK = [(FOREST, 60)]

# ServerHello on 2026-09-15 reported mode "Full"; the server was started
# with --single-user (noted in "source" below).
SERVER_IDENTITY = {
    "server_version": "0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode; data digests match "
                      "the signed manifest",
    "observed_at": "2026-09-15",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run's session under runs/run-20260915-7174b/; "
              "ServerHello re-checked by this run.",
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


def on_battlefield(state, oid):
    o = objects(state).get(str(oid)) or {}
    return o.get("zone") == "Battlefield"


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
    # mana-capable untapped lands only: Foothills has no mana ability, so it
    # cannot help cast Bill ({1}{G}); Triome taps for BGU (green included)
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and oname(o) in (FOREST, TRIOME) and not o.get("tapped"))


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
            # skip the shell wrapper's own cmdline (unexpanded $RUN_DIR);
            # only accept a db path that really points at a runs/<id> dir
            if "phase-server" in line and "--games-db" in line \
                    and "$RUN_DIR" not in line:
                parts = line.split()
                try:
                    i = parts.index("--games-db")
                    db = parts[i + 1]
                except (ValueError, IndexError):
                    continue
                if "/runs/" in db:
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


ST = {"stage": "SETUP", "bill_oid": None,
      "bill_cast": False, "bill_cast_turn": None,
      "ctrl1_oid": None, "ctrl2_oid": None,
      "c_before_ctrl1": None, "c_after_ctrl1": None,
      "c_before_ctrl2": None, "c_after_ctrl2": None,
      "c_before_f1": None, "c_after_f1": None,
      "c_before_f2": None, "c_after_f2": None,
      "search_answered": False, "search_want": TRIOME,
      "search_choice1": None, "search_choice2": None,
      "pre_exported": False, "mid1_exported": False,
      "mid2_exported": False, "post_exported": False,
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
           ("A1_parse", "A2_setup_ok", "A3_ctrl1", "A3b_ctrl2",
            "A4_triome", "A5_forest", "A6_cleanup")}

    hello = await check_server_hello()
    if hello:
        for k in ("server_version", "build_commit", "protocol_version"):
            if hello.get(k):
                SERVER_IDENTITY[k] = hello[k]

    # ---- A1: parse check against the pinned v0.84.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.84.0/data/card-data.json"))
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
        lands = sum(1 for n in names if n in (FOREST, FOOTHILLS, TRIOME))
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
            if nm == TRIOME:
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
        """Answer the fetch's library search by picking ST['search_want']
        (Zagoth Triome for the first fetch leg, Forest for the second)."""
        wt = (wf_type(state) or "").lower()
        if not ("search" in wt or "library" in wt):
            return False
        if wf_player(state) != 0:
            return False
        if ST["search_answered"]:
            return False
        want = ST["search_want"]
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            chs, _rtype = vi_choices(opp)
            if not chs:
                continue
            pick = None
            for ch in chs:
                if choice_card_name(ch, state) == want:
                    pick = ch
                    break
            if pick is None:
                say(f"[{tag}] WARNING: {want} not among search candidates; "
                    f"first 5: {[choice_card_name(ch, state) for ch in chs[:5]]}")
                continue
            leg = 1 if ST["stage"] == "F1_SEARCH" else 2
            ST["search_choice%d" % leg] = choice_text(pick)[:120]
            say(f"[{tag}] search pick (leg {leg}): "
                f"{ST['search_choice%d' % leg]}")
            await answer_vi(c, opp, pick, tag)
            ST["search_answered"] = True
            ST["stage"] = "F1_TRIG" if ST["stage"] == "F1_SEARCH" else "F2_TRIG"
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
            if ST["stage"] in ("CTRL1_WAIT", "CTRL2_WAIT",
                               "F1_TRIG", "F2_TRIG"):
                if await handle_bill_target(c, st, state, tag):
                    return
            # fetch search
            if ST["stage"] in ("F1_SEARCH", "F2_SEARCH"):
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
            if ST["stage"] == "SETUP" and boid is not None:
                ST["stage"] = "CTRL1"
                say(f"[P0] Bill on BF (oid {boid}); stage -> CTRL1")

            async def play_foothills(stage_next, before_key, oid_key):
                for oid in hand_oids(state, 0):
                    if oname(objects(state).get(str(oid), {})) != FOOTHILLS:
                        continue
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if (a.get("type") == "PlayLand"
                                and str(d.get("object_id", "")) == str(oid)):
                            ST[before_key] = counters_of(state, boid)
                            ST[oid_key] = oid
                            await submit_as_is(c, a)
                            ST["stage"] = stage_next
                            say(f"[P0] plays Foothills (oid {oid}, "
                                f"turn {turn}); bill counters "
                                f"before={ST[before_key]}")
                            return True
                return False

            # control leg 1: play Foothills#1 from hand
            if ST["stage"] == "CTRL1" and boid is not None:
                if await play_foothills("CTRL1_WAIT", "c_before_ctrl1",
                                        "ctrl1_oid"):
                    return
            # control leg 2: play Foothills#2 from hand
            if ST["stage"] == "CTRL2" and boid is not None:
                if await play_foothills("CTRL2_WAIT", "c_before_ctrl2",
                                        "ctrl2_oid"):
                    return
            # fetch leg 1: crack Foothills#1 -> search for Zagoth Triome
            # (the reporter's primary case)
            if ST["stage"] == "F1_ACT" and boid is not None:
                foid = ST["ctrl1_oid"]
                if foid is not None and on_battlefield(state, foid):
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if (a.get("type") == "ActivateAbility"
                                and d.get("source_id") == foid):
                            if not ST["pre_exported"]:
                                await export_named("pre")
                                ST["pre_exported"] = True
                            ST["c_before_f1"] = counters_of(state, boid)
                            ST["search_want"] = TRIOME
                            ST["search_answered"] = False
                            wire("fetch1_activate",
                                 {"turn": turn, "foothills": foid})
                            await submit_as_is(c, a)
                            ST["stage"] = "F1_SEARCH"
                            say(f"[P0] activates Foothills#1 (turn {turn}); "
                                f"bill counters before={ST['c_before_f1']}")
                            return
            # fetch leg 2: crack Foothills#2 -> search for a Forest
            if ST["stage"] == "F2_ACT" and boid is not None:
                foid = ST["ctrl2_oid"]
                if foid is not None and on_battlefield(state, foid):
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if (a.get("type") == "ActivateAbility"
                                and d.get("source_id") == foid):
                            ST["c_before_f2"] = counters_of(state, boid)
                            ST["search_want"] = FOREST
                            ST["search_answered"] = False
                            wire("fetch2_activate",
                                 {"turn": turn, "foothills": foid})
                            await submit_as_is(c, a)
                            ST["stage"] = "F2_SEARCH"
                            say(f"[P0] activates Foothills#2 (turn {turn}); "
                                f"bill counters before={ST['c_before_f2']}")
                            return
            # land drops: SETUP/CTRL stages drop Forests only (keep
            # Foothills in hand); no land drops during the fetch stages.
            if ST["stage"] in ("SETUP", "CTRL1", "CTRL2"):
                for a in acts:
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id")
                    if (a.get("type") == "PlayLand" and oid is not None
                            and oname(objects(state).get(str(oid), {}))
                            == FOREST):
                        await submit_as_is(c, a)
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
        for fn in ("pre", "mid1", "mid2", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid1, mid2, post = (states.get("pre"), states.get("mid1"),
                                 states.get("mid2"), states.get("post"))

        boid = ST["bill_oid"]

        def delta(before_key, after_key):
            b, a = ST[before_key], ST[after_key]
            if b is None or a is None:
                return None
            return a - b

        # ---- A2: setup ----
        if pre is not None and boid is not None:
            pb = bf_named(pre, 0, BILL)
            ok = (pb is not None and ST["c_after_ctrl1"] is not None
                  and ST["c_after_ctrl2"] is not None)
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: Bill on P0 BF in pre.json={pb is not None}, "
                         f"ctrl1 resolved={ST['c_after_ctrl1'] is not None}, "
                         f"ctrl2 resolved={ST['c_after_ctrl2'] is not None}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing or Bill never on BF")

        # ---- A3 / A3b: control legs (playing each fetch from hand) ----
        d1 = delta("c_before_ctrl1", "c_after_ctrl1")
        if d1 is not None:
            ass["A3_ctrl1"] = "passed" if d1 == 1 else "failed"
            notes.append(f"A3_ctrl1: playing Foothills#1 counter delta={d1} "
                         f"(expected 1)")
        else:
            ass["A3_ctrl1"] = "failed"
            notes.append("A3_ctrl1 failed: counter values missing")
        d2 = delta("c_before_ctrl2", "c_after_ctrl2")
        if d2 is not None:
            ass["A3b_ctrl2"] = "passed" if d2 == 1 else "failed"
            notes.append(f"A3b_ctrl2: playing Foothills#2 counter delta={d2} "
                         f"(expected 1)")
        else:
            ass["A3b_ctrl2"] = "failed"
            notes.append("A3b_ctrl2 failed: counter values missing")

        # ---- A4: the reported bug, Triome leg (reporter's primary case) ----
        df1 = delta("c_before_f1", "c_after_f1")
        if df1 is not None:
            ass["A4_triome"] = "passed" if df1 == 1 else "failed"
            notes.append(f"A4_triome: cracking Foothills#1 (Zagoth Triome "
                         f"entering) counter delta={df1} (expected 1; "
                         f"2 = the reported double-trigger); "
                         f"search_choice={ST['search_choice1']}")
        else:
            ass["A4_triome"] = "failed"
            notes.append("A4_triome failed: counter values missing")

        # ---- A5: the reported bug, Forest leg ----
        df2 = delta("c_before_f2", "c_after_f2")
        if df2 is not None:
            ass["A5_forest"] = "passed" if df2 == 1 else "failed"
            notes.append(f"A5_forest: cracking Foothills#2 (Forest entering) "
                         f"counter delta={df2} (expected 1; 2 = the reported "
                         f"double-trigger); "
                         f"search_choice={ST['search_choice2']}")
        else:
            ass["A5_forest"] = "failed"
            notes.append("A5_forest failed: counter values missing")

        # ---- A6: cleanup ----
        if post is not None:
            empty = len(stack_entries(post)) == 0
            ass["A6_cleanup"] = "passed" if empty else "failed"
            notes.append(f"A6: post stack empty={empty}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if all(ass[k] == "passed" for k in
               ("A1_parse", "A2_setup_ok", "A3_ctrl1", "A3b_ctrl2")):
            if ass["A4_triome"] == "passed" and ass["A5_forest"] == "passed":
                verdict = "not-reproduced"
            elif df1 == 2 or df2 == 2:
                verdict = "reproduced"
                notes.append(f"verdict=reproduced: fetch counter delta "
                             f"triome={df1} forest={df2}; 2 = the reported "
                             f"double-trigger")
            else:
                verdict = "blocked"
                notes.append(f"verdict blocked: fetch deltas triome={df1} "
                             f"forest={df2} are neither the correct 1 nor "
                             f"the reported 2")
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
            "scope": ("Bill landfall trigger count for (a) playing two "
                      "fetch lands from hand [control legs] and (b) cracking "
                      "each fetch: once fetching Zagoth Triome (the "
                      "reporter's primary case), once fetching a basic "
                      "Forest [reported bug]; native engine, two "
                      "human-client seats, P1 fully passive"),
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "bill_oid": boid,
                "bill_cast_turn": ST["bill_cast_turn"],
                "ctrl1_oid": ST["ctrl1_oid"], "ctrl2_oid": ST["ctrl2_oid"],
                "c_before_ctrl1": ST["c_before_ctrl1"],
                "c_after_ctrl1": ST["c_after_ctrl1"],
                "c_before_ctrl2": ST["c_before_ctrl2"],
                "c_after_ctrl2": ST["c_after_ctrl2"],
                "c_before_f1": ST["c_before_f1"],
                "c_after_f1": ST["c_after_f1"],
                "c_before_f2": ST["c_before_f2"],
                "c_after_f2": ST["c_after_f2"],
                "search_choice1": ST.get("search_choice1"),
                "search_choice2": ST.get("search_choice2"),
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
            "server_run_note": ("isolated v0.84.0 server on "
                                "127.0.0.1:9374 started by this run's session "
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
        files = ["pre.json", "mid1.json", "mid2.json", "post.json",
                 "parse_bill.json", "parse_foothills.json", "run.json",
                 "scenario_7174.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
        lines = []
        missing = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                missing.append(fn)
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # NOTE: no say() from here on - scenario_run.log is part of the
        # manifest, so further log appends would invalidate it. Missing
        # files go to console only.
        for fn in missing:
            print(f"manifest: MISSING {fn}", flush=True)
        print("wrote manifest.sha256", flush=True)
        for fn in ("pre.json", "mid1.json", "mid2.json", "post.json",
                   "parse_bill.json", "parse_foothills.json", "run.json"):
            if os.path.exists(f"{EVDIR}/{fn}"):
                json.load(open(f"{EVDIR}/{fn}"))
        for fn in ("mid1.json", "mid2.json"):
            if os.path.exists(f"{EVDIR}/{fn}"):
                json.load(open(f"{EVDIR}/{fn}"))
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
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15 - "
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

        def dlt(bk, ak):
            b, a = ds.get(bk), ds.get(ak)
            return (a - b) if b is not None and a is not None else "?"

        d.text((24, y), "control (play each fetch from hand), expected "
               "delta 1 each:", fill=(200, 210, 225))
        y += 24
        d.text((40, y), f"Foothills#1: {ds['c_before_ctrl1']} -> "
               f"{ds['c_after_ctrl1']} "
               f"(delta {dlt('c_before_ctrl1', 'c_after_ctrl1')})",
               fill=(140, 160, 180))
        y += 24
        d.text((40, y), f"Foothills#2: {ds['c_before_ctrl2']} -> "
               f"{ds['c_after_ctrl2']} "
               f"(delta {dlt('c_before_ctrl2', 'c_after_ctrl2')})",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), "crack each fetch, expected delta 1 (2 = reported "
               "bug):", fill=(200, 210, 225))
        y += 24
        d.text((40, y), f"fetch#1 (Zagoth Triome): {ds['c_before_f1']} -> "
               f"{ds['c_after_f1']} "
               f"(delta {dlt('c_before_f1', 'c_after_f1')})",
               fill=(140, 160, 180))
        y += 24
        d.text((40, y), f"fetch#2 (Forest): {ds['c_before_f2']} -> "
               f"{ds['c_after_f2']} "
               f"(delta {dlt('c_before_f2', 'c_after_f2')})",
               fill=(140, 160, 180))
        y += 24
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
            if boid is not None and ST["stage"] in ("CTRL1_WAIT",
                                                    "CTRL2_WAIT",
                                                    "F1_TRIG", "F2_TRIG"):
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
                        if ST["stage"] == "F1_TRIG" \
                                and not ST["mid1_exported"]:
                            await export_named("mid1")
                            ST["mid1_exported"] = True
                        if ST["stage"] == "F2_TRIG" \
                                and not ST["mid2_exported"]:
                            await export_named("mid2")
                            ST["mid2_exported"] = True

            async def resolve_leg(wait_stage, before_key, after_key,
                                  next_stage, fh_oid_key=None,
                                  stable_key=None, tag="leg"):
                """Close a trigger window: counters rose past `before` with
                no Bill trigger left on the stack (and, for fetch legs, the
                cracked fetch gone from the battlefield); else a stable
                no-change watchdog."""
                if ST["stage"] != wait_stage or boid is None \
                        or ST[before_key] is None:
                    return
                c = counters_of(state, boid)
                trig_gone = not bill_trigger_entries(state, boid)
                fh_gone = (fh_oid_key is None
                           or not on_battlefield(state, ST[fh_oid_key]))
                if c > ST[before_key] and trig_gone and fh_gone:
                    ST[after_key] = c
                    ST["stage"] = next_stage
                    say(f"{tag} trigger resolved: counters "
                        f"{ST[before_key]} -> {c}")
                    wire(f"{tag}_resolved",
                         {"before": ST[before_key], "after": c,
                          "turn": turn, "phase": phase,
                          "sightings": len(ST["trigger_sightings"])})
                elif c == ST[before_key] and trig_gone and fh_gone:
                    n = ST.get(stable_key, 0) + 1
                    ST[stable_key] = n
                    if n >= 40:
                        ST[after_key] = c
                        ST["stage"] = next_stage
                        say(f"{tag} window closed stable with no counter "
                            f"change (delta 0)")
                        wire(f"{tag}_stable_close",
                             {"before": ST[before_key], "after": c,
                              "turn": turn, "phase": phase,
                              "sightings": len(ST["trigger_sightings"])})
                else:
                    ST[stable_key] = 0

            # control leg 1 resolution -> CTRL2
            await resolve_leg("CTRL1_WAIT", "c_before_ctrl1",
                              "c_after_ctrl1", "CTRL2",
                              stable_key="ctrl1_stable", tag="ctrl1")
            # control leg 2 resolution -> F1_ACT
            await resolve_leg("CTRL2_WAIT", "c_before_ctrl2",
                              "c_after_ctrl2", "F1_ACT",
                              stable_key="ctrl2_stable", tag="ctrl2")
            # fetch leg 1 (Triome) resolution -> F2_ACT
            await resolve_leg("F1_TRIG", "c_before_f1", "c_after_f1",
                              "F2_ACT", fh_oid_key="ctrl1_oid",
                              stable_key="f1_stable", tag="fetch1")
            # fetch leg 2 (Forest) resolution -> DONE
            await resolve_leg("F2_TRIG", "c_before_f2", "c_after_f2",
                              "DONE", fh_oid_key="ctrl2_oid",
                              stable_key="f2_stable", tag="fetch2")
            if ST["stage"] == "DONE":
                ST["done"] = True
                say("all legs complete")
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
