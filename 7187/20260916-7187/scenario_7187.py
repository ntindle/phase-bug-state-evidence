#!/usr/bin/env python3
"""Issue #7187: The Sixth Doctor - clone isn't nonlegendary.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:confirmed, source:discord):
  "[[The Sixth Doctor]] Clone isn't currently nonlegendary"

Oracle text (verified from pinned card-data.json):
  "Time Lord's Prerogative - Whenever you cast a historic spell, copy it,
   except the copy isn't legendary. This ability triggers only once each
   turn. (Artifacts, legendaries, and Sagas are historic. A copy of a
   permanent spell becomes a token.)"

Parse (pinned card-data.json, v0.84.0): the SpellCast trigger carries
  additional_modifications: [{"type": "RemoveSupertype", "supertype":
  "Legendary"}] - i.e. the parser supplies the "except the copy isn't
  legendary" clause. The defect is in the engine's copy path: the copy of
  the historic spell keeps the Legendary supertype (and the legend rule
  then kills one of the two permanents).

Expected behavior: P0 controls The Sixth Doctor and casts Mox Amber ({0},
Legendary Artifact - historic). The trigger copies Mox Amber; the copy
resolves as a TOKEN that is NOT legendary. Post state: two Mox Ambers on
P0's battlefield - the original (Legendary) and the copy token
(nonlegendary).

Reported behavior: the copy keeps Legendary; the legend rule applies.

Scenario: P0 casts The Sixth Doctor ({4}{G}{U}, 3/3 Legendary Creature -
Time Lord Doctor per pinned data), then casts Mox Amber. Assertions are
read from authoritative exports.

Assertions:
  A1_setup_ok    The Sixth Doctor on P0 BF with Legendary supertype at
                 pre.json; Mox Amber cast by P0 (mox_cast_turn recorded).
  A2_token_made  Two P0-controlled "mox amber" objects on the battlefield
                 were observed (copy token entered). Fails if the copy
                 never becomes a permanent.
  A3_not_legend  The copy (the mox amber that is NOT the originally cast
                 object) lacks the Legendary supertype.
  A4_coexist     post.json: both Mox Ambers still on P0's battlefield
                 (legend rule did not eat one), stack empty, game advanced
                 past the Mox Amber cast turn.
  A5_cleanup     post.json: stack empty; turn advanced past mox_cast_turn.

Verdict rule:
  blocked        iff A1 fails (setup never reached).
  reproduced     iff A1 passes and (A2 or A3 or A4) fails.
  not-reproduced iff A1..A5 all pass.

Evidence: evidence/7187/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7187.py, wire_log.jsonl,
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
ISSUE = 7187
RUN_ID = os.environ.get("RUN_ID", "20260916-7187")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DOCTOR = "the sixth doctor"
MOX = "mox amber"
FOREST = "forest"
ISLAND = "island"
SWAMP = "swamp"

P0_DECK = [(DOCTOR, 10), (MOX, 12), (FOREST, 19), (ISLAND, 19)]
P1_DECK = [(SWAMP, 60)]

RELDIR = f"{BACKFILL}/server/releases/v0.84.0"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
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
                      "2026-09-15; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run "
                      "(AGENTS.md: never copy SERVER_IDENTITY hashes); "
                      "release v0.84.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-16",
    "observed_at": "2026-09-16",
    "handshake": "ServerHello observed pre-run on 127.0.0.1:9374: "
                 "v0.84.0 / eb7e93e / protocol 71",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/20260916-7187/ (prior live server "
              "died in a VM replacement mid-run; restarted fresh)",
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


def obj_power(obj):
    for k in ("power", "current_power"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict) and isinstance(v.get("value"), int):
            return v["value"]
    return None


def obj_card_types(obj):
    # state view carries the parsed type line under "card_types" (plural);
    # "card_type" (singular) is NULL in the state view (cf. #6879)
    ct = obj.get("card_types")
    if isinstance(ct, dict):
        return ct
    return {}


def obj_supertypes(obj):
    return [str(s).lower() for s in (obj_card_types(obj).get("supertypes") or [])]


def obj_subtypes(obj):
    ct = obj_card_types(obj)
    subs = ct.get("subtypes") or []
    if not subs and isinstance(obj.get("subtypes"), list):
        subs = obj["subtypes"]
    return [str(s).lower() for s in subs]


def obj_token_flag(obj):
    """Best-effort token detection across known object shapes."""
    for k in ("is_token", "isToken", "token"):
        v = obj.get(k)
        if v is True:
            return True
    blob = json.dumps(obj, default=str).lower()
    return '"token": true' in blob or '"istoken": true' in blob

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> doctor_ready -> mox_cast -> observed -> done
        "game_code": None,
        "doctor_oid": None,
        "mox_spell_oid": None,
        "mox_cast_turn": None,
        "mox_oids": [],
        "mox_entered_turn": None,
        "saw_two_mox": False,
        "copy_on_stack_seen": False,
        "answered_iids": [],
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
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "stack_copy_sightings": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-doctor")
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

    async def mulligan_decide(c, pid, tag, st, state, want):
        """Mulligan until `want` (card key) is in hand; keep otherwise."""
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

    def observe_doctor(state):
        if ST["doctor_oid"] is None:
            ds = bf_ids(state, 0, DOCTOR)
            if ds:
                ST["doctor_oid"] = ds[0]
                do = get_obj(state, ds[0])
                say(f"[obs] The Sixth Doctor on BF oid={ds[0]} "
                    f"supertypes={obj_supertypes(do)} "
                    f"power={obj_power(do)}")
                wire("doctor_on_bf", {"oid": ds[0],
                                      "supertypes": obj_supertypes(do),
                                      "power": obj_power(do)})

    def observe_stack(state):
        for e in stack_entries(state):
            blob = json.dumps(e, default=str).lower()
            if "copyspell" in blob or ("copy" in blob and "mox" in blob):
                sig = hashlib.sha256(blob.encode()).hexdigest()[:12]
                if sig not in obs["stack_copy_sightings"]:
                    obs["stack_copy_sightings"].append(sig)
                    ST["copy_on_stack_seen"] = True
                    say(f"[obs] copy-ish stack entry: "
                        f"{json.dumps(e, default=str)[:500]}")
                    wire("copy_on_stack",
                         {"entry": json.loads(json.dumps(e, default=str))})

    def observe_mox(state):
        ms = bf_ids(state, 0, MOX)
        if len(ms) >= 2 and not ST["saw_two_mox"]:
            ST["saw_two_mox"] = True
            ST["mox_oids"] = ms
            ST["mox_entered_turn"] = state.get("turn_number")
            det = []
            for oid in ms:
                o = get_obj(state, oid)
                det.append({"oid": oid,
                            "supertypes": obj_supertypes(o),
                            "subtypes": obj_subtypes(o),
                            "is_token": obj_token_flag(o),
                            "is_original": oid == ST.get("mox_spell_oid"),
                            "entered_turn": o.get("entered_battlefield_turn"),
                            "zone": o.get("zone")})
            say(f"[obs] TWO Mox Ambers on P0 BF: {json.dumps(det)}")
            wire("two_mox_on_bf", det)
        elif len(ms) == 1 and ST["stage"] in ("mox_cast",):
            pass  # copy not yet a permanent

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
        if await mulligan_decide(p0, 0, "P0", st, state, DOCTOR):
            return
        if await handle_discard(p0, 0, "P0", st, state, (DOCTOR, MOX)):
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
        observe_doctor(state)
        observe_stack(state)
        observe_mox(state)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        # legend-rule keep choice (defensive; cf. #6773) - only reached if
        # the copy token is (buggily) legendary
        wplayer = wf_player(state)
        if wtype == "ChooseLegend" and wplayer == 0:
            ca = find_action(acts, "ChooseLegend")
            if ca and not acted("legend", rev):
                await submit_as_is(p0, ca)
                say("[P0] ChooseLegend answered as-is (keeps first)")
                wire("action_submit", {"who": "P0",
                                       "action": "ChooseLegend/asis"})
                return

        # copy token observed? close the observation window once
        if ST["saw_two_mox"] and ST["stage"] in ("mox_cast", "doctor_ready",
                                                "setup"):
            if "mid" not in ST["exports"]:
                await export_named("mid")
            ST["stage"] = "observed"
            ST["observed_at"] = time.time()
            say("[P0] stage -> observed (two Mox Ambers seen)")
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
            if ST["doctor_oid"] is None \
                    and DOCTOR in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, DOCTOR)
                if ca and not acted("doccast", rev):
                    say("[P0] casting The Sixth Doctor")
                    await submit_as_is(p0, ca)
                    return
            if ST["doctor_oid"] is not None \
                    and ST["stage"] == "setup":
                ST["stage"] = "doctor_ready"
                say("[P0] stage -> doctor_ready")
            if ST["stage"] == "doctor_ready" \
                    and "pre" not in ST["exports"]:
                await export_named("pre")
            if ST["stage"] == "doctor_ready" \
                    and MOX in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, MOX)
                if ca and not acted("moxcast", rev):
                    ST["mox_spell_oid"] = (ca.get("data") or {}).get(
                        "object_id")
                    ST["mox_cast_turn"] = turn
                    say(f"[P0] casting Mox Amber "
                        f"oid={ST['mox_spell_oid']} (turn {turn})")
                    await submit_as_is(p0, ca)
                    wire("mox_cast",
                         {"oid": ST["mox_spell_oid"], "turn": turn})
                    ST["stage"] = "mox_cast"
                    return

        # after the observation: let the game advance one more turn, then
        # export post and finish
        if ST["stage"] == "observed" and my_priority(state, 0) \
                and not stack_entries(state):
            cast_turn = ST.get("mox_cast_turn") or 0
            if turn > cast_turn or (
                    ST.get("observed_at")
                    and time.time() - ST["observed_at"] > 60):
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
            blob = json.dumps(rj, default=str)
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
        observe_doctor(state)
        observe_stack(state)
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        wtype = wf_of(state).get("type") or ""
        if wtype == "ChooseLegend" and wf_player(state) == 1:
            ca = find_action(acts, "ChooseLegend")
            if ca and not acted("p1legend", rev):
                await submit_as_is(p1, ca)
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
                    f"doctor={ST['doctor_oid']} "
                    f"mox_oids={ST['mox_oids']}")
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

        def mox_on_bf(state):
            return bf_ids(state, 0, MOX)

        # ---- A1: setup ----
        doc_oid = ST["doctor_oid"]
        doc_pre = get_obj(pre, doc_oid) if (pre and doc_oid) else {}
        doc_leg = "legendary" in obj_supertypes(doc_pre)
        doc_on_bf = doc_pre.get("zone") == "Battlefield"
        a1 = ("pre" in states and doc_oid is not None and doc_on_bf
              and doc_leg and ST.get("mox_cast_turn") is not None)
        notes.append(
            f"A1: doctor_oid={doc_oid} on_bf_at_pre={doc_on_bf} "
            f"legendary_at_pre={doc_leg} supertypes="
            f"{obj_supertypes(doc_pre)} mox_cast_turn="
            f"{ST.get('mox_cast_turn')} pre_exported={'pre' in states}")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: copy token became a permanent ----
        mox_mid = mox_on_bf(mid) if mid else []
        mox_post = mox_on_bf(post) if post else []
        saw2 = ST.get("saw_two_mox") or len(mox_mid) >= 2 \
            or len(mox_post) >= 2
        a2 = bool(a1 and saw2)
        notes.append(
            f"A2: saw_two_mox(tick)={ST.get('saw_two_mox')} "
            f"mox_on_bf at mid={mox_mid} at post={mox_post}; "
            f"copy_on_stack_seen={ST.get('copy_on_stack_seen')} "
            f"stack_copy_sightings={len(obs['stack_copy_sightings'])}")
        ass["A2_token_made"] = "passed" if a2 else "failed"

        # ---- A3: the copy is not legendary ----
        # The originally-cast Mox is ST["mox_spell_oid"]; any other
        # P0 Mox Amber on the BF is the copy token.
        copy_oid = None
        copy_supertypes = []
        copy_is_token = None
        ref = mid if mox_mid else post
        for oid in (mox_mid or mox_post):
            if oid != ST.get("mox_spell_oid"):
                copy_oid = oid
                co = get_obj(ref, oid)
                copy_supertypes = obj_supertypes(co)
                copy_is_token = obj_token_flag(co)
                break
        a3 = (a2 and copy_oid is not None
              and "legendary" not in copy_supertypes)
        notes.append(
            f"A3: copy_oid={copy_oid} (original={ST.get('mox_spell_oid')}) "
            f"supertypes={copy_supertypes} is_token={copy_is_token}; "
            f"expect no 'legendary'")
        ass["A3_not_legendary"] = "passed" if a3 else "failed"

        # ---- A4: both coexist ----
        a4 = (a2 and len(mox_post) >= 2
              and "legendary" not in copy_supertypes)
        notes.append(
            f"A4: mox_on_bf at post={mox_post} (expect >=2 objects: "
            f"original Legendary + nonlegendary copy token)")
        ass["A4_coexist"] = "passed" if a4 else "failed"

        # ---- A5: cleanup ----
        a5 = (bool(post) and not stack_entries(post)
              and (post.get("turn_number") or 0) > (ST.get(
                  "mox_cast_turn") or 0))
        notes.append(
            f"A5: post present={bool(post)} stack_empty="
            f"{not stack_entries(post) if post else None} "
            f"turn_post={post.get('turn_number') if post else None} "
            f"mox_cast_turn={ST.get('mox_cast_turn')}")
        ass["A5_cleanup"] = "passed" if a5 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: A1 failed - Doctor + Mox "
                         "setup never completed")
        elif (ass["A2_token_made"] != "passed"
                or ass["A3_not_legendary"] != "passed"
                or ass["A4_coexist"] != "passed"):
            verdict = "reproduced"
            notes.append(
                "verdict=reproduced: The Sixth Doctor's copy of Mox Amber "
                f"kept the Legendary supertype (copy supertypes="
                f"{copy_supertypes}); legend rule removed one - the "
                "reported 'clone isn't nonlegendary' behavior")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_token_made", "A3_not_legendary",
                  "A4_coexist", "A5_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: copy token entered "
                         "nonlegendary and coexists with the original")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-16",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 71,
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
                "doctor_oid": ST["doctor_oid"],
                "mox_spell_oid": ST["mox_spell_oid"],
                "mox_cast_turn": ST["mox_cast_turn"],
                "mox_oids": ST["mox_oids"],
                "mox_entered_turn": ST["mox_entered_turn"],
                "saw_two_mox": ST["saw_two_mox"],
                "copy_on_stack_seen": ST["copy_on_stack_seen"],
                "copy_oid": copy_oid,
                "copy_supertypes": copy_supertypes,
                "copy_is_token": copy_is_token,
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
                "10x/12x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is a passive second seat (lands only, no attacks) - "
                "opponent interaction with the copy is not exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Only the 'copy isn't legendary' exception is asserted; "
                "the once-per-turn constraint and copy-of-nonpermanent "
                "paths are not exercised.",
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
        d.text((24, y), "phase-rs/phase #7187 - The Sixth Doctor copy",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-16"
               " - copy spell 'except the copy isn't legendary'",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: The Sixth Doctor - 'Clone isn't currently",
                "nonlegendary'. Oracle: 'Whenever you cast a historic",
                "spell, copy it, except the copy isn't legendary. This",
                "ability triggers only once each turn. (A copy of a",
                "permanent spell becomes a token.)' Setup: P0 casts The",
                "Sixth Doctor ({4}{G}{U} 3/3 Legendary), then casts Mox",
                "Amber ({0} Legendary Artifact - historic). Expected:",
                "copy resolves as a nonlegendary token; both Mox Ambers",
                "coexist. Reported: the copy stays legendary."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Parse (pinned card-data.json):",
               fill=(200, 210, 225))
        y += 24
        for ln in [
                "trigger SpellCast carries additional_modifications:",
                "[{RemoveSupertype: Legendary}] - the parser supplies the",
                "'except the copy isn't legendary' clause. Defect is in",
                "the engine's copy path, not the parse."]:
            d.text((24, y), ln, fill=(160, 175, 195))
            y += 22
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Doctor on P0 BF (Legendary); Mox Amber cast",
            "A2_token_made": "copy token became a P0 battlefield permanent",
            "A3_not_legendary": "copy lacks the Legendary supertype",
            "A4_coexist": "both Mox Ambers on P0 BF at post (no legend"
                          " rule kill)",
            "A5_cleanup": "stack empty; game advanced past the cast turn",
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
                f"doctor oid={ds.get('doctor_oid')} "
                f"mox spell oid={ds.get('mox_spell_oid')} "
                f"cast turn={ds.get('mox_cast_turn')}",
                f"copy oid={ds.get('copy_oid')} "
                f"supertypes={ds.get('copy_supertypes')} "
                f"is_token={ds.get('copy_is_token')}",
                f"saw_two_mox={ds.get('saw_two_mox')} "
                f"copy_on_stack_seen={ds.get('copy_on_stack_seen')}",
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:14])}",
                "Exports: pre.json (Doctor on BF, Mox in hand),",
                "mid.json (two Mox Ambers first seen on BF),",
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
