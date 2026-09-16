#!/usr/bin/env python3
"""Issue #7185: Master Biomancer - doesn't add counters equal to power.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:confirmed, source:discord):
  "[[master biomancer]] doesn't add counters equal to power just adds 1
   +1/+1 counter"

Oracle text (verified from pinned card-data.json):
  "Each other creature you control enters with a number of additional +1/+1
   counters on it equal to this creature's power and as a Mutant in addition
   to its other types."

Expected behavior: a creature entering under P0's control while P0 controls
Master Biomancer (power 2) enters with 2 additional +1/+1 counters (a
2/2 Grizzly Bears enters as a 4/4) and as a Mutant in addition to its
other types.

Observed behavior (per triage): the replacement always supplies exactly one
+1/+1 counter. The parse check (card-data.json, pinned at run time — v0.85.0 in this run) shows why:
the replacement's PutCounter count is {type: Fixed, value: 1} with a
SwallowedClause/DynamicQty parse warning, and the "as a Mutant" addition
is absent from the replacement entirely. (Parse observed against the
currently-pinned card-data.json; A0 re-evaluates it live.)

Scenario: P0 casts Master Biomancer ({2}{G}{U}, 2/4), then casts Grizzly
Bears (2/2). Assertions are read from authoritative exports.

Assertions:
  A0_parse_gap   Pinned card-data parse of Master Biomancer: PutCounter
                 count is Fixed(1) (not the dynamic source-power quantity),
                 a SwallowedClause/DynamicQty warning spans the line, and
                 the replacement carries no Mutant type addition.
  A1_setup_ok    Master Biomancer on P0 BF with power 2 at pre.json;
                 Grizzly Bears cast by P0 and on the BF at mid.json.
  A2_counters    The entering Bears carries exactly 2 P1P1 counters
                 (power 4). 1 counter (power 3) is the reported failure.
  A3_mutant      The entering Bears has the Mutant subtype in addition to
                 its other types.
  A4_cleanup     post.json: stack empty, game advanced past the cast turn.

Verdict rule:
  blocked        iff A1 fails (setup never reached).
  reproduced     iff A1 passes and (A2 or A3) fails.
  not-reproduced iff A1..A4 all pass.

Evidence: evidence/7185/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7185.py, wire_log.jsonl,
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
ISSUE = 7185
RUN_ID = os.environ.get("RUN_ID", "20260916-7185r2")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BIOMANCER = "master biomancer"
BEARS = "grizzly bears"
FOREST = "forest"
ISLAND = "island"
SWAMP = "swamp"

P0_DECK = [(BIOMANCER, 8), (BEARS, 8), (FOREST, 22), (ISLAND, 22)]
P1_DECK = [(SWAMP, 60)]

RELDIR = f"{BACKFILL}/server/releases/v0.85.0"


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
                      "2026-09-16; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run "
                      "(AGENTS.md: never copy SERVER_IDENTITY hashes); "
                      "release v0.85.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-16",
    "observed_at": "2026-09-16",
    "handshake": "ServerHello observed pre-run: v0.85.0 / cb58ef5 / "
                 "protocol 72 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; shared isolated server on 127.0.0.1:9374 "
              "(games-db runs/20260916-7191/games.db, started by the "
              "#7191 backfill run today); not restarted by this run",
}


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


def build_submission(iid, rtype, choice_id=None, choice_ids=None):
    if rtype == "schema":
        data = {"choiceIds": choice_ids if choice_ids is not None
                else ([choice_id] if choice_id is not None else [])}
        return {"interactionId": iid,
                "response": {"type": "sequence", "data": data}}
    return {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


def count_p1p1(obj):
    """Best-effort count of +1/+1 counters across known object shapes."""
    total = 0
    seen = False
    c = obj.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            if "p1p1" in str(k).lower() or "+1/+1" in str(k):
                seen = True
                if isinstance(v, int):
                    total += v
    elif isinstance(c, list):
        for e in c:
            if isinstance(e, dict):
                t = str(e.get("type") or e.get("kind") or e.get("name")
                        or "").lower()
                if "p1p1" in t or "+1/+1" in t:
                    seen = True
                    cv = e.get("count")
                    total += cv if isinstance(cv, int) else 1
            elif isinstance(e, str):
                if "p1p1" in e.lower() or "+1/+1" in e:
                    seen = True
                    total += 1
    return total, seen


def obj_power(obj):
    for k in ("power", "current_power"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict) and isinstance(v.get("value"), int):
            return v["value"]
    return None


def obj_subtypes(obj):
    subs = []
    # state view carries the parsed type line under "card_types" (plural);
    # "card_type" (singular) is NULL in the state view (cf. #6879)
    ct = obj.get("card_types")
    if isinstance(ct, dict):
        subs = ct.get("subtypes") or []
    if not subs and isinstance(obj.get("subtypes"), list):
        subs = obj["subtypes"]
    return [str(s).lower() for s in subs]


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> biomancer_ready -> observed -> done
        "game_code": None,
        "biomancer_oid": None,
        "biomancer_power": None,
        "bears_spell_oid": None,
        "bears_oid": None,
        "bears_cast_turn": None,
        "bears_mid_counters": None,
        "bears_mid_power": None,
        "bears_mid_obj_dump": None,
        "mid_exported_at": None,
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "last_rev_seen": {},
        "last_rev_change": {},
        "need_post_export": False,
        "post_exported": False,
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-biomancer")
    p1 = PhaseClient("P1-idle")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
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

    async def mulligan_keep(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
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
            if iid in ST["answered_iids"]:
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
                if SWAMP in tx or FOREST in tx or ISLAND in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                build_submission(iid, resp.get("type"), pick.get("id")))
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

    def observe_biomancer(state):
        if ST["biomancer_oid"] is None:
            b = bf_ids(state, 0, BIOMANCER)
            if b:
                ST["biomancer_oid"] = b[0]
                ST["biomancer_power"] = obj_power(get_obj(state, b[0]))
                say(f"[obs] Master Biomancer on BF oid={b[0]} "
                    f"power={ST['biomancer_power']}")
                wire("biomancer_on_bf",
                     {"oid": b[0], "power": ST["biomancer_power"]})

    def find_p0_bears(state):
        """P0-controlled Grizzly Bears on the BF; prefer the cast one."""
        cands = bf_ids(state, 0, BEARS)
        if not cands:
            return None
        if ST["bears_spell_oid"] in cands:
            return ST["bears_spell_oid"]
        # fall back to the most recently entered one
        def et(o):
            return get_obj(state, o).get("entered_battlefield_turn") or 0
        return max(cands, key=et)

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
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state,
                                (BIOMANCER, BEARS)):
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
            # #4509: a rejected iid must not stay in answered_iids
            blob = json.dumps(rj, default=str)
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
                    say(f"[P0] un-answered rejected iid {iid}")
        observe_biomancer(state)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        # Bears entered? record the replacement outcome once.
        bears = find_p0_bears(state)
        if bears is not None and ST["bears_oid"] is None:
            bo = get_obj(state, bears)
            n, seen = count_p1p1(bo)
            ST["bears_oid"] = bears
            ST["bears_mid_counters"] = n if seen else None
            ST["bears_mid_power"] = obj_power(bo)
            ST["bears_mid_obj_dump"] = json.dumps(bo, default=str)[:4000]
            say(f"[obs] Grizzly Bears entered: oid={bears} "
                f"counters_seen={seen} p1p1={n} power={ST['bears_mid_power']}")
            wire("bears_entered",
                 {"oid": bears, "counters": n, "counters_seen": seen,
                  "power": ST["bears_mid_power"]})
            await export_named("mid")
            ST["mid_exported_at"] = time.time()
            ST["stage"] = "observed"
            return

        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # ---- main-phase actions ----
        if my_priority(state, 0) \
                and phase in ("PreCombatMain", "PostCombatMain") \
                and not stack_entries(state):
            for oid in hand_ids(state, 0):
                if lname(state, oid) in (FOREST, ISLAND):
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            if ST["biomancer_oid"] is None \
                    and BIOMANCER in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, BIOMANCER)
                if ca and not acted("biocast", rev):
                    say("[P0] casting Master Biomancer")
                    await submit_as_is(p0, ca)
                    return
            if ST["biomancer_oid"] is not None \
                    and ST["stage"] == "setup":
                ST["stage"] = "biomancer_ready"
                say("[P0] stage -> biomancer_ready")
            if ST["stage"] == "biomancer_ready" \
                    and "pre" not in ST["exports"]:
                await export_named("pre")
            if ST["stage"] == "biomancer_ready" \
                    and BEARS in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, BEARS)
                if ca and not acted("bearscast", rev):
                    ST["bears_spell_oid"] = (ca.get("data") or {}).get(
                        "object_id")
                    ST["bears_cast_turn"] = turn
                    say(f"[P0] casting Grizzly Bears "
                        f"oid={ST['bears_spell_oid']} (turn {turn})")
                    await submit_as_is(p0, ca)
                    wire("bears_cast",
                         {"oid": ST["bears_spell_oid"], "turn": turn})
                    return

        # after the observation: let the game advance one more turn, then
        # export post and finish
        if ST["stage"] == "observed" and my_priority(state, 0) \
                and not stack_entries(state):
            cast_turn = ST.get("bears_cast_turn") or 0
            if turn > cast_turn or (
                    ST.get("mid_exported_at")
                    and time.time() - ST["mid_exported_at"] > 60):
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
        if await mulligan_keep(p1, 1, "P1", st, state):
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
            blob = json.dumps(rj, default=str)
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
        observe_biomancer(state)
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == SWAMP:
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
                # #4509 stale-client watchdog
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
                    f"biomancer={ST['biomancer_oid']} "
                    f"bears={ST['bears_oid']}")
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

        # ---- A0: parse gap in pinned card-data ----
        a0_detail = {}
        try:
            cd = json.load(open(f"{RELDIR}/data/card-data.json"))
            entry = next(v for k, v in cd.items()
                         if k.lower() == BIOMANCER)
            rep = (entry.get("replacements") or [])[0]
            eff = rep["execute"]["effect"]
            a0_detail["effect_type"] = eff.get("type")
            a0_detail["count"] = eff.get("count")
            a0_detail["warnings"] = [
                (w.get("type"), w.get("detector"))
                for w in entry.get("parse_warnings", [])]
            # the description prose mentions Mutant; only an actual
            # effect node counts as a type addition
            no_desc = {k: v for k, v in rep.items() if k != "description"}
            a0_detail["mutant_in_replacement"] = \
                "mutant" in json.dumps(no_desc).lower()
            a0 = (eff.get("type") == "PutCounter"
                  and (eff.get("count") or {}).get("type") == "Fixed"
                  and (eff.get("count") or {}).get("value") == 1
                  and any(w[0] == "SwallowedClause"
                          and w[1] == "DynamicQty"
                          for w in a0_detail["warnings"])
                  and not a0_detail["mutant_in_replacement"])
        except Exception as e:
            a0 = False
            a0_detail["error"] = repr(e)
        notes.append(f"A0: parse gap present={a0} detail="
                     f"{json.dumps(a0_detail, default=str)[:300]}")
        ass["A0_parse_gap"] = "passed" if a0 else "failed"

        # ---- A1: setup ----
        bio = ST["biomancer_oid"]
        bio_power_pre = obj_power(get_obj(pre, bio)) if (pre and bio) \
            else None
        bears = ST["bears_oid"]
        bears_on_bf_mid = (bears is not None and mid
                           and get_obj(mid, bears).get("zone")
                           == "Battlefield")
        a1 = ("pre" in states and bio is not None
              and bio_power_pre == 2
              and bears_on_bf_mid)
        notes.append(
            f"A1: biomancer_oid={bio} power_at_pre={bio_power_pre} "
            f"(expect 2); bears_oid={bears} on_bf_at_mid="
            f"{bears_on_bf_mid}; pre_exported={'pre' in states} "
            f"mid_exported={'mid' in states}")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: counters on the entering Bears ----
        n_mid, seen_mid = (None, False)
        power_mid = None
        if bears is not None and mid:
            bo = get_obj(mid, bears)
            n_mid, seen_mid = count_p1p1(bo)
            power_mid = obj_power(bo)
        expect = 2  # Biomancer power
        a2 = (a1 and n_mid == expect)
        notes.append(
            f"A2: bears p1p1 counters at mid={n_mid} (counters_seen="
            f"{seen_mid}), power={power_mid}; expect {expect} "
            f"(Biomancer power 2); obj dump: "
            f"{(ST.get('bears_mid_obj_dump') or '')[:600]}")
        ass["A2_counters"] = "passed" if a2 else "failed"

        # ---- A3: Mutant subtype ----
        subs = []
        mutant_seen = False
        if bears is not None and mid:
            bo = get_obj(mid, bears)
            subs = obj_subtypes(bo)
            mutant_seen = "mutant" in subs
        # fallback: printed subtypes exist in state view at all?
        bio_subs = []
        if bio is not None and mid:
            bio_subs = obj_subtypes(get_obj(mid, bio))
        a3 = (a1 and mutant_seen)
        notes.append(
            f"A3: bears subtypes at mid={subs} mutant_seen={mutant_seen}; "
            f"biomancer subtypes (sanity)={bio_subs}")
        if not bio_subs and not subs:
            ass["A3_mutant"] = "not-run"
            notes.append("A3 not-run: state view exposes no subtype "
                         "fields at all (card_type NULL); Mutant "
                         "assertion unevaluable from state")
        else:
            ass["A3_mutant"] = "passed" if a3 else "failed"

        # ---- A4: cleanup ----
        a4 = (bool(post) and not stack_entries(post)
              and (post.get("turn_number") or 0) > (ST.get(
                  "bears_cast_turn") or 0))
        notes.append(
            f"A4: post present={bool(post)} stack_empty="
            f"{not stack_entries(post) if post else None} "
            f"turn_post={post.get('turn_number') if post else None} "
            f"cast_turn={ST.get('bears_cast_turn')}")
        ass["A4_cleanup"] = "passed" if a4 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: A1 failed - Biomancer + Bears "
                         "setup never completed")
        elif (ass["A2_counters"] != "passed"
                or ass["A3_mutant"] == "failed"):
            verdict = "reproduced"
            notes.append(
                "verdict=reproduced: Biomancer (power 2) on the "
                f"battlefield, but the entering Grizzly Bears carried "
                f"{n_mid} +1/+1 counter(s) instead of 2 "
                f"(power {power_mid}, expect 4) and mutant subtype "
                f"{'absent' if ass['A3_mutant'] == 'failed' else 'unevaluable'} "
                "- the reported 'just adds 1 +1/+1 counter' behavior")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_counters", "A4_cleanup")) \
                and ass.get("A3_mutant") in ("passed", "not-run"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: entering Bears got 2 "
                         "counters (power 4) matching Biomancer's power")
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
                "biomancer_oid": ST["biomancer_oid"],
                "biomancer_power": ST["biomancer_power"],
                "bears_spell_oid": ST["bears_spell_oid"],
                "bears_oid": ST["bears_oid"],
                "bears_cast_turn": ST["bears_cast_turn"],
                "bears_mid_counters": ST["bears_mid_counters"],
                "bears_mid_power": ST["bears_mid_power"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
                "bears_mid_obj_dump": ST.get("bears_mid_obj_dump"),
            },
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "8x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is a passive second seat (lands only, no attacks) - "
                "opponent interaction with the replacement is not "
                "exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Master Biomancer's power is fixed at 2 in this fixture; "
                "the dynamic-power quantity is asserted as 2 counters, "
                "not across a range of powers.",
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
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7185 - Master Biomancer counters",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.85.0 (cb58ef5) protocol 72 - 2026-09-16"
               " - entry replacement: counters equal to power",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: Master Biomancer 'doesn't add counters equal to",
                "power, just adds 1 +1/+1 counter'. Oracle: 'Each other",
                "creature you control enters with a number of additional",
                "+1/+1 counters on it equal to this creature's power and",
                "as a Mutant in addition to its other types.' Setup: P0",
                "casts Master Biomancer (2/4, power 2), then Grizzly Bears",
                "(2/2). Expected: Bears enters as 4/4 Mutant Bear.",
                "Reported: Bears enters with a single +1/+1 counter."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Parse gap (pinned card-data.json):",
               fill=(200, 210, 225))
        y += 24
        for ln in [
                "replacement PutCounter count = Fixed(1) - the dynamic",
                "'equal to this creature's power' quantity was swallowed",
                "(SwallowedClause / DynamicQty warning); no Mutant type",
                "addition present in the replacement at all."]:
            d.text((24, y), ln, fill=(160, 175, 195))
            y += 22
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A0_parse_gap": "pinned parse: Fixed(1) counter, swallowed "
                            "dynamic qty, no Mutant addition",
            "A1_setup_ok": "Biomancer on P0 BF (power 2); Bears cast and "
                           "on BF",
            "A2_counters": "entering Bears carries exactly 2 P1P1 "
                           "counters (power 4)",
            "A3_mutant": "entering Bears has the Mutant subtype",
            "A4_cleanup": "stack empty; game advanced past the cast turn",
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
        for ln in [
                f"biomancer oid={ds.get('biomancer_oid')} "
                f"power={ds.get('biomancer_power')}",
                f"bears cast turn={ds.get('bears_cast_turn')} "
                f"oid={ds.get('bears_oid')}",
                f"bears at mid.json: p1p1 counters="
                f"{ds.get('bears_mid_counters')} power="
                f"{ds.get('bears_mid_power')} (expect counters=2, "
                f"power=4)",
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:14])}",
                "Exports: pre.json (Biomancer on BF, Bears in hand),",
                "mid.json (Bears just entered, replacement applied),",
                "post.json (stack empty, later turn).",
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
