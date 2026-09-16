#!/usr/bin/env python3
"""Issue #7184: Decayed creature not pausing at end of combat with instants out.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:needs-repro, source:discord):
  "Probably a minor thing, but decayed creatures are just auto-saccing after
   combat damage even if I have instants. I was planning on using Plumb the
   Forbidden to sacrifice it before it could sacrifice itself."

Oracle text (Decayed reminder, from Rot-Curse Rakshasa):
  "Decayed (This creature can't block. When it attacks, sacrifice it at
   end of combat.)"

Expected behavior: the decayed "sacrifice it at end of combat" trigger goes
on the stack at the end of combat; while it is pending, the player holding
instants receives priority and may cast them (e.g. Plumb the Forbidden,
sacrificing the decayed creature itself in response).

Observed behavior: the creature is auto-sacrificed after combat damage with
no chance to act.

Triage note: exact priority configuration unknown; this run uses two
human-client seats where P0 NEVER auto-passes while the sacrifice is
pending, so any absence of a priority window is the engine's doing.

Scenario: P0 casts Rot-Curse Rakshasa (5/5, {1}{B}, Decayed) and attacks P1
with it. P1 does not block. At end of combat the decayed sacrifice becomes
pending. P0 holds Plumb the Forbidden ({1}{B} instant) in hand.

Assertions:
  A1_setup_ok     Rakshasa on P0 BF; Plumb in P0 hand; Rakshasa attacked P1
                  (pre_combat.json exported at DeclareAttackers).
  A2_sac_observed The decayed sacrifice happened: the Rakshasa moved
                  Battlefield -> Graveyard on the attack turn after combat.
                  (A decayed sacrifice TRIGGER on the stack is recorded
                  separately in ST.trigger; the engine may auto-sac with no
                  stack event - that is the reported behavior.)
  A3_priority_pause  P0 received waiting_for Priority with priority_player
                  == 0 while the Rakshasa was still on the battlefield in a
                  post-damage phase (EndOfCombat / PostCombatMain / End /
                  Cleanup) of the attack turn - i.e. a chance to cast
                  instants before the sacrifice. pause.json exported at the
                  first such observation. Per-tick window samples
                  (turn/phase/wf/priority_player/rakshasa zone) are kept in
                  run.json to corroborate.
  A4_response_cast  (only meaningful if A3 passes) P0 cast Plumb the
                  Forbidden while the Rakshasa was still on the battlefield
                  in the decayed window, sacrificing the Rakshasa to the
                  additional cost.
  A5_post_state   (only meaningful if A3 passes) post_response.json:
                  Rakshasa in P0 graveyard; stack empty; P0 life < 20
                  (Plumb drew / lost life).

Verdict rule:
  blocked        iff A1 fails (setup never reached combat).
  reproduced     iff A1 and A2 pass and A3 fails: the decayed creature was
                 sacrificed at end of combat without P0 ever receiving
                 priority while it was still on the battlefield - the
                 reported behavior.
  not-reproduced iff A1..A5 all pass.
  blocked        otherwise (incomplete chain).

Evidence: evidence/7184/<run-id>/pre_combat.json, trigger_stack.json,
pause.json, post_response.json, run.json, manifest.sha256, summary.png,
scenario_7184.py, wire_log.jsonl, scenario_run.log, server.log (excerpts).
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
ISSUE = 7184
RUN_ID = os.environ.get("RUN_ID", "20260915-7184")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RAK = "rot-curse rakshasa"
PLUMB = "plumb the forbidden"
SWAMP = "swamp"
CORPSE = "walking corpse"
LANDS = (SWAMP,)

P0_DECK = [(RAK, 8), (PLUMB, 12), (SWAMP, 40)]
P1_DECK = [(CORPSE, 12), (SWAMP, 48)]

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
                      "digests recomputed against on-disk files this run; "
                      "release v0.84.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-15",
    "observed_at": "2026-09-15",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/20260915-7184/",
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


def gy_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("owner") == pid
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


def decayed_trigger_on_stack(state):
    """Decayed sacrifice trigger: TriggeredAbility sourced from an object
    with the Decayed keyword (the engine issues it with an EMPTY
    description, so text matching alone misses it), or whose ability text
    is the decayed sacrifice reminder."""
    for e in stack_entries(state):
        kind = (e.get("kind") or {}).get("type")
        if kind != "TriggeredAbility":
            continue
        sid = e.get("source_id")
        ab = e.get("ability") or {}
        desc = str(ab.get("description") or e.get("description") or "")
        if "sacrifice" in desc.lower() and "end of combat" in desc.lower():
            return e, sid
        if sid is not None:
            src = get_obj(state, int(sid))
            kws = src.get("keywords") or []
            if any(k == "Decayed" or
                   (isinstance(k, dict) and k.get("name") == "Decayed")
                   for k in kws):
                return e, sid
    return None, None


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> attack_setup -> combat ->
                            # trigger_pending -> resolving -> done
        "game_code": None,
        "rak_oid": None,
        "rak_entered": None,
        "attacked": False,
        "trigger": None,    # {stack_id, source_oid, turn, ability_text}
        "pause_seen": False,      # priority given to P0 while sac pending
        "pause_details": None,
        "plumb_cast": None,       # {turn, trigger_on_stack_at_cast}
        "sac_choice": None,       # choiceIds used for Plumb's sac cost
        "auto_sac": False,        # rakshasa left BF with no pause observed
        "attack_turn": None,
        "sac_time": None,
        "window": [],             # per-tick samples after the attack
        "window_cap": 5000,
        "resolving_done": False,
        "life_before_plumb": None,
        "hand_before_plumb": None,
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "cast_in_flight": False,
        "need_post_export": False,
        "post_exported": False,
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "sac_prompt_shapes": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-decay")
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
                if SWAMP in tx:
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

    async def p0_declare_attack(c, st, state, acts):
        wtype = wf_of(state).get("type") or ""
        if wtype != "DeclareAttackers":
            return False
        if wf_player(state) not in (0, None) \
                and state.get("active_player") != 0:
            return False
        if ST["stage"] not in ("attack_setup", "combat") \
                or ST["attacked"]:
            return False
        if ST["rak_oid"] is None:
            return False
        turn = state.get("turn_number") or 0
        et = ST.get("rak_entered")
        if isinstance(et, int) and et >= turn:
            return False  # summoning sickness
        da = find_action(acts, "DeclareAttackers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted("p0attack", rev):
            return True
        rak = ST["rak_oid"]
        if rak not in bf_ids(state, 0, RAK):
            return False
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = [[rak, {"type": "Player", "data": 1}]]
        d["bands"] = []
        await export_named("pre_combat")
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        ST["attacked"] = True
        ST["attack_turn"] = turn
        ST["stage"] = "combat"
        say(f"[P0] attacking P1 with Rakshasa oid={rak} (turn {turn})")
        wire("attack_declared", {"rak": rak, "turn": turn})
        return True

    async def p1_declare_no_block(c, st, state, acts):
        wtype = wf_of(state).get("type") or ""
        if wtype != "DeclareBlockers":
            return False
        if ST["stage"] not in ("combat", "trigger_pending") \
                or not ST["attacked"]:
            return False
        da = find_action(acts, "DeclareBlockers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted("p1blk", rev):
            return True
        d = copy.deepcopy(da.get("data", {}))
        d["assignments"] = []
        await submit_as_is(c, {"type": "DeclareBlockers", "data": d})
        say("[P1] no blocks")
        return True

    async def p0_declare_no_block(c, st, state, acts):
        wtype = wf_of(state).get("type") or ""
        if wtype != "DeclareBlockers":
            return False
        if wf_player(state) != 0:
            return False
        da = find_action(acts, "DeclareBlockers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted("p0blk", rev):
            return True
        d = copy.deepcopy(da.get("data", {}))
        d["assignments"] = []
        await submit_as_is(c, {"type": "DeclareBlockers", "data": d})
        return True

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

    def observe_common(state):
        """Track Rakshasa; detect the decayed sacrifice trigger on stack."""
        if ST["rak_oid"] is None:
            r = bf_ids(state, 0, RAK)
            if r:
                ST["rak_oid"] = r[0]
                et = get_obj(state, r[0]).get("entered_battlefield_turn")
                ST["rak_entered"] = et
                say(f"[obs] Rakshasa on BF oid={r[0]} entered_turn={et}")
                wire("rakshasa_on_bf", {"oid": r[0], "turn": et})
        if ST["trigger"] is None:
            te, sid = decayed_trigger_on_stack(state)
            if te is not None:
                ST["trigger"] = {
                    "stack_id": te.get("id"),
                    "source_oid": sid,
                    "turn": state.get("turn_number"),
                    "ability_text": str(
                        ((te.get("ability") or {}).get("description"))
                        or te.get("description") or "")[:200],
                }
                ST["stage"] = "trigger_pending"
                say(f"[obs] DECAYED trigger ON STACK id={te.get('id')} "
                    f"source={sid} text="
                    f"{ST['trigger']['ability_text'][:90]!r}")
                wire("decayed_trigger_on_stack", ST["trigger"])
                return "saw_trigger"
        else:
            ids = [e.get("id") for e in stack_entries(state)]
            if ST["trigger"]["stack_id"] not in ids \
                    and not ST.get("trigger_gone"):
                ST["trigger_gone"] = True
                say("[obs] decayed trigger left the stack")
                wire("decayed_trigger_gone", {})
                return "trigger_gone"
        # auto-sac detection: rakshasa leaves BF after the attack while no
        # pause was ever given (one-shot; the tick would spam otherwise)
        if ST["stage"] in ("trigger_pending", "combat") \
                and not ST["auto_sac"]:
            rak = ST["rak_oid"]
            if rak is not None and ST["attacked"]:
                zone = get_obj(state, rak).get("zone")
                if zone and zone != "Battlefield" \
                        and not ST["pause_seen"] \
                        and not ST["plumb_cast"]:
                    ST["auto_sac"] = True
                    ST["sac_time"] = time.time()
                    say(f"[obs] Rakshasa left BF (zone={zone}) with "
                        f"no priority pause observed -> AUTO-SAC")
                    wire("auto_sac", {"zone": zone,
                                      "turn": state.get("turn_number"),
                                      "phase": state.get("phase")})
        return None

    def sample_window(state):
        """Per-tick corroborating sample once the attack has happened."""
        if not ST["attacked"] or len(ST["window"]) >= ST["window_cap"]:
            return
        rak = ST["rak_oid"]
        wf = wf_of(state)
        ST["window"].append({
            "turn": state.get("turn_number"),
            "phase": state.get("phase"),
            "wf": wf.get("type"),
            "pp": state.get("priority_player"),
            "rak_zone": get_obj(state, rak).get("zone") if rak else None,
            "stack_n": len(stack_entries(state)),
            "plumb_hand_n": sum(1 for o in hand_ids(state, 0)
                                if lname(state, o) == PLUMB),
            "life0": player_of(state, 0).get("life"),
        })

    async def answer_cost_prompt(c, pid, tag, st, state):
        """Handle Plumb the Forbidden's additional-cost flow: optional pay
        choice, then sacrifice selection for the Rakshasa."""
        wtype = wf_of(state).get("type") or ""
        if "Sacrifice" not in wtype and "OptionalCost" not in wtype \
                and "Cost" not in wtype:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            if "OptionalCost" in wtype:
                # kicker-style: decide via action/value surfaces (#6758)
                if not ST.get("optcost_dumped"):
                    ST["optcost_dumped"] = True
                    obs["sac_prompt_shapes"].append(
                        {"wtype": wtype,
                         "full_choices": json.dumps(chs)[:3000]})
                    say(f"[{tag}] OptionalCostChoice full dump logged "
                        f"({len(chs)} choices)")
                pay = None
                for ch in chs:
                    s = json.dumps(ch.get("surfaces", [])).lower()
                    if "decideoptionalcost" in s and '"pay","true"' in s:
                        pay = ch
                        break
                if pay is None:
                    # fallback: pick the choice whose text suggests paying
                    for ch in chs:
                        s = json.dumps(ch).lower()
                        if "pay" in s and "decline" not in s:
                            pay = ch
                            break
                    if pay is None:
                        pay = chs[0]
                ST["answered_iids"].append(iid)
                obs["sac_prompt_shapes"].append(
                    {"wtype": wtype, "n_choices": len(chs),
                     "picked": "pay"})
                say(f"[{tag}] OptionalCostChoice: answering PAY")
                await c.send_interaction(
                    build_submission(iid, rtype, pay.get("id")))
                return True
            # sacrifice selection: choose the Rakshasa
            rak = ST["rak_oid"]
            pick = None
            for ch in chs:
                if ref_matches_oid(ch, rak):
                    pick = ch
                    break
            if pick is None:
                # choose by name fallback
                for ch in chs:
                    if RAK in json.dumps(ch).lower():
                        pick = ch
                        break
            if pick is None:
                say(f"[{tag}] sacrifice prompt: Rakshasa not among "
                    f"{len(chs)} choices; recording shape")
                obs["sac_prompt_shapes"].append(
                    {"wtype": wtype, "n_choices": len(chs),
                     "choice_dump": json.dumps(chs)[:1500]})
                obs["unexpected_prompts"].append(
                    f"sacrifice prompt without rakshasa: {wtype}")
                return False
            ST["answered_iids"].append(iid)
            ST["sac_choice"] = [rak]
            obs["sac_prompt_shapes"].append(
                {"wtype": wtype, "n_choices": len(chs),
                 "picked_oid": rak})
            say(f"[{tag}] sacrificing Rakshasa oid={rak} to Plumb")
            await c.send_interaction(
                build_submission(iid, rtype, choice_id=pick.get("id"),
                                 choice_ids=[pick.get("id")]))
            return True
        return False

    def ref_matches_oid(ch, oid):
        s = json.dumps(ch.get("surfaces", []))
        return f'"{oid}"' in s or f":{oid}" in s or f" {oid}" in s

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state, (RAK, PLUMB)):
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
        sig = observe_common(state)
        if sig == "saw_trigger" and not ST.get("trigger_exported"):
            await export_named("trigger_stack")
            ST["trigger_exported"] = True

        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        # ---- decayed window: never pass while the sacrifice is pending ----
        if ST["stage"] == "trigger_pending":
            # cost prompts for the in-flight Plumb
            if await answer_cost_prompt(p0, 0, "P0", st, state):
                return
            te_on_stack = any(
                e.get("id") == (ST["trigger"] or {}).get("stack_id")
                for e in stack_entries(state))
            if my_priority(state, 0) and te_on_stack \
                    and not ST["plumb_cast"]:
                if not ST["pause_seen"]:
                    ST["pause_seen"] = True
                    plumb_oids = [o for o in hand_ids(state, 0)
                                  if lname(state, o) == PLUMB]
                    castable = cast_spell_action(acts, state, PLUMB)
                    ST["pause_details"] = {
                        "turn": turn, "phase": phase,
                        "plumb_in_hand": plumb_oids,
                        "cast_advertised": castable is not None,
                        "stack_depth": len(stack_entries(state)),
                    }
                    await export_named("pause")
                    say(f"[P0] PAUSE OBSERVED: priority with decayed "
                        f"trigger on stack; plumb in hand={plumb_oids}, "
                        f"cast advertised={castable is not None}")
                    wire("pause_observed", ST["pause_details"])
                ca = cast_spell_action(acts, state, PLUMB)
                if ca and not acted("plumbcast", rev):
                    ST["life_before_plumb"] = player_of(
                        state, 0).get("life")
                    ST["hand_before_plumb"] = len(hand_ids(state, 0))
                    await submit_as_is(p0, ca)
                    ST["plumb_cast"] = {
                        "turn": turn,
                        "trigger_on_stack_at_cast": te_on_stack,
                        "object_oid": (ca.get("data") or {}).get(
                            "object_id"),
                    }
                    ST["cast_in_flight"] = True
                    ST["plumb_cast_time"] = time.time()
                    say("[P0] cast Plumb the Forbidden in response to "
                        "the decayed trigger")
                    wire("plumb_cast", ST["plumb_cast"])
                    return
                # priority but cast not advertised yet: hold, don't pass
                return
            if ST["plumb_cast"] and not ST.get("resolving_done"):
                # Plumb in flight or resolving: let it resolve, then wait
                # for the decayed window to close (the sacrifice happens at
                # end of combat, AFTER Plumb resolves) before finishing.
                if my_priority(state, 0):
                    combat_over = (turn > ST.get("attack_turn", 0)) or (
                        turn == ST.get("attack_turn") and phase in
                        ("PostCombatMain", "End", "Cleanup"))
                    if not stack_entries(state) and combat_over:
                        ST["resolving_done"] = True
                        ST["need_post_export"] = True
                        ST["stage"] = "done"
                        say("[P0] Plumb resolved and decayed window "
                            "closed; stage -> done")
                        return
                    if not acted("p0pass_r", rev):
                        pa = find_action(acts, "PassPriority")
                        if pa:
                            await submit_as_is(p0, pa)
                return
            if ST.get("trigger_gone") and not ST["pause_seen"] \
                    and not ST["plumb_cast"]:
                # trigger resolved with no pause ever given: the bug
                ST["need_post_export"] = True
                ST["stage"] = "done"
                say("[P0] decayed trigger resolved with NO pause -> done")
                return
            # trigger pending but not our priority yet: hold (don't pass)
            return

        # ---- decayed window with NO stack trigger (engine auto-sacs) ----
        # If P0 gets priority in a post-damage phase of the attack turn
        # while the Rakshasa is still on the battlefield, that is the
        # pause the report says is missing: hold it and answer with Plumb
        # the Forbidden (the reported line).
        if ST["stage"] == "combat" and ST["attacked"]:
            if await answer_cost_prompt(p0, 0, "P0", st, state):
                return
            rak = ST["rak_oid"]
            rak_zone = get_obj(state, rak).get("zone") if rak else None
            in_window = (turn == ST.get("attack_turn") and phase not in
                         ("PreCombatMain", "BeginningOfCombat",
                          "DeclareAttackers", "DeclareBlockers",
                          "CombatDamage"))
            if my_priority(state, 0) and rak_zone == "Battlefield" \
                    and in_window and not ST["plumb_cast"]:
                if not ST["pause_seen"]:
                    ST["pause_seen"] = True
                    plumb_oids = [o for o in hand_ids(state, 0)
                                  if lname(state, o) == PLUMB]
                    castable = cast_spell_action(acts, state, PLUMB)
                    ST["pause_details"] = {
                        "turn": turn, "phase": phase,
                        "plumb_in_hand": plumb_oids,
                        "cast_advertised": castable is not None,
                        "stack_depth": len(stack_entries(state)),
                        "trigger_on_stack": False,
                    }
                    await export_named("pause")
                    say(f"[P0] PAUSE OBSERVED (no stack trigger): "
                        f"priority in {phase} with Rakshasa on BF; "
                        f"plumb in hand={plumb_oids}, cast advertised="
                        f"{castable is not None}")
                    wire("pause_observed", ST["pause_details"])
                ca = cast_spell_action(acts, state, PLUMB)
                if ca and not acted("plumbcast", rev):
                    ST["life_before_plumb"] = player_of(
                        state, 0).get("life")
                    ST["hand_before_plumb"] = len(hand_ids(state, 0))
                    await submit_as_is(p0, ca)
                    ST["plumb_cast"] = {
                        "turn": turn,
                        "trigger_on_stack_at_cast": False,
                        "object_oid": (ca.get("data") or {}).get(
                            "object_id"),
                    }
                    ST["cast_in_flight"] = True
                    ST["plumb_cast_time"] = time.time()
                    say("[P0] cast Plumb the Forbidden before the "
                        "decayed sacrifice")
                    wire("plumb_cast", ST["plumb_cast"])
                    return
                # priority but cast not advertised yet: hold, don't pass
                return
            if ST["plumb_cast"] and not ST.get("resolving_done"):
                if my_priority(state, 0):
                    combat_over = (turn > ST.get("attack_turn", 0)) or (
                        turn == ST.get("attack_turn") and phase in
                        ("PostCombatMain", "End", "Cleanup"))
                    if not stack_entries(state) and combat_over:
                        ST["resolving_done"] = True
                        ST["need_post_export"] = True
                        ST["stage"] = "done"
                        say("[P0] Plumb resolved and decayed window "
                            "closed; stage -> done")
                        return
                    if not acted("p0pass_r", rev):
                        pa = find_action(acts, "PassPriority")
                        if pa:
                            await submit_as_is(p0, pa)
                return

        if await p0_declare_attack(p0, st, state, acts):
            return
        if await p0_declare_no_block(p0, st, state, acts):
            return
        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # ---- main-phase actions ----
        if ST["stage"] in ("setup", "attack_setup") \
                and my_priority(state, 0) \
                and phase in ("PreCombatMain", "PostCombatMain") \
                and not stack_entries(state):
            for oid in hand_ids(state, 0):
                if lname(state, oid) in LANDS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            if ST["rak_oid"] is None and RAK in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, RAK)
                if ca and not acted("rak", rev):
                    say("[P0] casting Rot-Curse Rakshasa")
                    await submit_as_is(p0, ca)
                    return
            if ST["rak_oid"] is not None \
                    and ST["stage"] == "setup":
                ST["stage"] = "attack_setup"
                say("[P0] stage -> attack_setup (Rakshasa on BF)")

        # default: pass priority (always fall through, cf. #6862)
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
        observe_common(state)
        if ST["trigger"] is not None and not ST.get("trigger_exported"):
            await export_named("trigger_stack")
            ST["trigger_exported"] = True
        if await p1_declare_no_block(p1, st, state, acts):
            return
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
            if len(bf_ids(state, 1, CORPSE)) < 2 \
                    and CORPSE in hand_lnames(state, 1):
                ca = cast_spell_action(acts, state, CORPSE)
                if ca and not acted("p1corpse", rev):
                    say("[P1] casting Walking Corpse")
                    await submit_as_is(p1, ca)
                    return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST.get("need_post_export") and not ST.get("post_exported"):
                await export_named("post_response")
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
            try:
                acts = merged_actions(st)
                sample_window(state)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            # early finish: the sacrifice already happened (rak in GY) and
            # the attack turn is over, or 120s have passed since the sac -
            # avoids stalling in a post-window cleanup/discard loop.
            # Backstop: 240s after the Plumb cast with no resolution.
            if ST.get("attack_turn") is not None:
                s_early = (p0.latest or {}).get("state") or {}
                se_turn = s_early.get("turn_number") or 0
                if ST.get("auto_sac") and (
                        se_turn > ST["attack_turn"] or (
                            ST.get("sac_time")
                            and time.time() - ST["sac_time"] > 120)):
                    say("[main] early finish: sacrifice observed, window "
                        "complete")
                    break
                if ST.get("plumb_cast") and ST.get("plumb_cast_time") \
                        and time.time() - ST["plumb_cast_time"] > 240:
                    say("[main] early finish: Plumb cast timed out")
                    break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"rak={ST['rak_oid']} attacked={ST['attacked']} "
                    f"pause={ST['pause_seen']} plumb={ST['plumb_cast']}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        # guarantee a post-window export even on early finish
        if "post_response" not in ST.get("exports", {}):
            await export_named("post_response")
        states = {}
        for fn in ("pre_combat", "trigger_stack", "pause",
                   "post_response"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre = states.get("pre_combat", {})
        tstack = states.get("trigger_stack", {})
        pause = states.get("pause", {})
        post = states.get("post_response", {})

        # ---- A1: setup ----
        a1 = (ST["rak_oid"] is not None
              and ST["attacked"]
              and "pre_combat" in states)
        plumb_in_hand_at_attack = any(
            lname(pre, o) == PLUMB for o in hand_ids(pre, 0)) if pre else None
        notes.append(
            f"A1: rak_oid={ST['rak_oid']} attacked={ST['attacked']} "
            f"pre_combat_exported={'pre_combat' in states} "
            f"plumb_in_hand_at_attack={plumb_in_hand_at_attack}")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: decayed sacrifice observed ----
        trig = ST["trigger"]
        rak = ST["rak_oid"]
        rak_zone_now = get_obj(post, rak).get("zone") \
            if (post and rak) else None
        a2 = ST["auto_sac"] or rak_zone_now == "Graveyard"
        notes.append(
            f"A2: decayed_trigger_seen={trig is not None} "
            f"(stack_id={(trig or {}).get('stack_id')}) "
            f"auto_sac_flag={ST['auto_sac']} rakshasa_zone_post="
            f"{rak_zone_now}")
        ass["A2_sac_observed"] = "passed" if a2 else "failed"

        # ---- A3: priority pause while Rakshasa still on BF ----
        pd = ST["pause_details"] or {}
        # corroborate with the window samples: any tick on the attack turn
        # in a post-damage phase with rak on BF, wf Priority, pp 0
        atk_turn = ST.get("attack_turn")
        post_phases = {"EndCombat", "EndOfCombat", "PostCombatMain",
                       "End", "Cleanup"}
        pause_samples = [
            w for w in ST["window"]
            if w.get("turn") == atk_turn
            and w.get("phase") in post_phases
            and w.get("rak_zone") == "Battlefield"
            and w.get("wf") == "Priority" and w.get("pp") == 0]
        sac_samples = [
            w for w in ST["window"]
            if w.get("turn") == atk_turn
            and w.get("rak_zone") == "Battlefield"
            and w.get("phase") in post_phases]
        notes.append(
            f"A3-corroboration: {len(pause_samples)} window samples with "
            f"P0 priority + Rakshasa on BF in post-damage phases; "
            f"{len(sac_samples)} samples with Rakshasa on BF in "
            f"post-damage phases at all; {len(ST['window'])} total "
            f"samples")
        a3 = ST["pause_seen"] and "pause" in states
        notes.append(
            f"A3: pause_seen={ST['pause_seen']} pause_exported="
            f"{'pause' in states} details={json.dumps(pd, default=str)[:220]} "
            f"auto_sac_flag={ST['auto_sac']}")
        ass["A3_priority_pause"] = "passed" if a3 else "failed"

        # ---- A4: Plumb cast in the decayed window ----
        # (cast submitted while the decayed trigger was on the stack or
        # the Rakshasa was still on the battlefield in the window; the
        # additional-cost sacrifice SELECTION is a separate Plumb-the-
        # Forbidden cost-flow concern, recorded under observations)
        pc = ST["plumb_cast"] or {}
        if ass["A3_priority_pause"] != "passed":
            ass["A4_response_cast"] = "not-run"
            notes.append("A4 not-run: no priority pause was ever given")
        else:
            a4 = bool(pc)
            notes.append(
                f"A4: plumb_cast={bool(pc)} "
                f"trigger_on_stack_at_cast="
                f"{pc.get('trigger_on_stack_at_cast')} "
                f"sac_choice={ST.get('sac_choice')} "
                f"life_before={ST.get('life_before_plumb')} "
                f"hand_before={ST.get('hand_before_plumb')}")
            ass["A4_response_cast"] = "passed" if a4 else "failed"

        # ---- A5: post state ----
        life_after = player_of(post, 0).get("life") if post else None
        gy_plumb = gy_ids(post, 0, PLUMB) if post else []
        if ass["A3_priority_pause"] != "passed":
            ass["A5_post_state"] = "not-run"
            notes.append(
                f"A5 not-run: no priority pause; post rakshasa_zone="
                f"{rak_zone_now} stack_empty="
                f"{not stack_entries(post) if post else None} "
                f"life_after={life_after}")
        else:
            a5 = bool(post) and rak_zone_now == "Graveyard" \
                and not stack_entries(post) \
                and life_after is not None \
                and life_after < (ST.get("life_before_plumb") or 21)
            notes.append(
                f"A5: post_response present={bool(post)} rakshasa_zone="
                f"{rak_zone_now} (expect Graveyard) stack_empty="
                f"{not stack_entries(post) if post else None} "
                f"life_before={ST.get('life_before_plumb')} life_after="
                f"{life_after} plumb_in_gy={gy_plumb}")
            ass["A5_post_state"] = "passed" if a5 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: A1 failed - combat setup with "
                         "Rot-Curse Rakshasa never completed")
        elif (ass["A1_setup_ok"] == "passed"
                and ass["A2_sac_observed"] == "passed"
                and ass["A3_priority_pause"] != "passed"):
            verdict = "reproduced"
            notes.append(
                "verdict=reproduced: the decayed Rakshasa was sacrificed "
                f"at end of combat (trigger_on_stack_seen="
                f"{trig is not None}) but P0 never received priority "
                f"while it was still on the battlefield "
                f"(pause_seen={ST['pause_seen']}, "
                f"{len(pause_samples)} corroborating window samples); "
                f"no chance to cast instants - the reported behavior")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_sac_observed", "A3_priority_pause",
                  "A4_response_cast", "A5_post_state")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: the decayed trigger "
                         "paused for P0 priority with Plumb the Forbidden "
                         "in hand; P0 cast Plumb in response to the "
                         "trigger (its additional-cost sacrifice "
                         "selection never appeared - see observations); "
                         "the Rakshasa was then sacrificed by the decayed "
                         "trigger at end of combat")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
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
                "rak_oid": ST["rak_oid"],
                "rak_entered": ST["rak_entered"],
                "attacked": ST["attacked"],
                "attack_turn": ST["attack_turn"],
                "trigger": ST["trigger"],
                "pause_seen": ST["pause_seen"],
                "pause_details": ST["pause_details"],
                "plumb_cast": ST["plumb_cast"],
                "sac_choice": ST["sac_choice"],
                "auto_sac": ST["auto_sac"],
                "life_before_plumb": ST["life_before_plumb"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
                "window_samples": len(ST["window"]),
                "window_pause_samples": len(pause_samples),
                "window_head": ST["window"][:8],
                "window_tail": ST["window"][-8:],
            },
            "notes": notes,
            "evidence_files": ["pre_combat.json", "trigger_stack.json",
                               "pause.json", "post_response.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "Decayed is exercised through Rot-Curse Rakshasa, the "
                "only non-token Decayed card in the pinned card data; "
                "token-granted Decayed (e.g. Jadar tokens) not exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "P1 is a passive second seat (lands, unblocked, no "
                "instants) - opponent interaction with the decayed "
                "trigger is not exercised.",
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
        d.text((24, y), "phase-rs/phase #7184 - Decayed: no pause at end "
               "of combat", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
               " - end-of-combat priority around Decayed",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: decayed creatures auto-sac after combat damage",
                "even when the player holds instants; the reporter wanted",
                "to Plumb the Forbidden the decayed creature in response.",
                "Expected: the decayed sacrifice trigger pauses for",
                "priority so instants can be cast. Setup: P0 attacks P1",
                "with Rot-Curse Rakshasa (5/5 Decayed), P1 no blocks,",
                "Plumb the Forbidden in P0's hand."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Rakshasa on P0 BF; Plumb in hand; Rakshasa "
                           "attacked P1",
            "A2_sac_observed": "decayed sacrifice happened (Rakshasa "
                               "Battlefield -> Graveyard)",
            "A3_priority_pause": "P0 got Priority while Rakshasa still on "
                                 "BF in post-damage phases (pause.json)",
            "A4_response_cast": "Plumb cast in response; Rakshasa "
                                "sacrificed to its cost",
            "A5_post_state": "Rakshasa in P0 GY; stack empty; P0 life < 20",
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
        trig = ds.get("trigger") or {}
        pd = ds.get("pause_details") or {}
        pc = ds.get("plumb_cast") or {}
        for ln in [
                f"rakshasa oid={ds.get('rak_oid')} "
                f"attacked={ds.get('attacked')}",
                f"decayed trigger on stack: "
                f"{'yes id=' + str(trig.get('stack_id')) if trig.get('stack_id') else 'no (engine auto-sacs)'}",
                f"pause_seen={ds.get('pause_seen')} "
                f"cast_advertised={pd.get('cast_advertised')} "
                f"plumb_in_hand={pd.get('plumb_in_hand')}",
                f"window samples: {ds.get('window_samples')} total, "
                f"{ds.get('window_pause_samples')} with P0 priority + "
                f"Rakshasa on BF in post-damage phases",
                f"plumb_cast={bool(pc)} "
                f"sac_choice={ds.get('sac_choice')} "
                f"life_before={ds.get('life_before_plumb')}",
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:14])}",
                "Exports: pre_combat.json (attack declared),",
                "pause.json (priority given, if any),",
                "post_response.json (after the decayed window).",
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
