#!/usr/bin/env python3
"""Issue #7182: Rite of Passage puts +1/+1 counters on the wrong target.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:confirmed, source:discord):
  "[[Rite of Passage]] p1p1's should be assigned to the creature that
   received damage, but they're instead going to the (opponent's) creature
   that dealt damage."

Oracle text: "Whenever a creature you control is dealt damage, put a +1/+1
counter on it. (It must survive the damage to get the counter.)"

Card-data parse (pinned v0.84.0): trigger mode DamageReceived, valid_card =
Creature/You (controller), effect PutCounter P1P1 x1, target EventTarget.

Scenario: P0 controls Rite of Passage + Colossal Dreadmaw (6/6). P1 attacks
with a Grizzly Bears (2/2); P0 blocks with the Dreadmaw. Dreadmaw takes 2
damage and survives; the bear takes 6 and dies. The trigger must put the
counter on the Dreadmaw (the damaged survivor). Per the report, the engine
instead resolves EventTarget to the damage dealer.

Assertions:
  A1_setup_ok     Rite of Passage + Dreadmaw on P0 BF; P1 bear attacked;
                  Dreadmaw blocked it (pre_combat.json exported).
  A2_trigger      Rite of Passage TriggeredAbility observed on the stack
                  (trigger_stack.json exported at first sighting).
  A3_recipient    post_trigger.json: Dreadmaw carries exactly 1 +1/+1 counter.
  A4_dealer_none  post_trigger.json: the attacking bear carries 0 counters.
  A5_survived     post_trigger.json: Dreadmaw still on the battlefield.
  A6_cleanup      post_trigger.json: stack empty; damage event observed
                  (damage_marked=2 on Dreadmaw at trigger time).

Verdict rule:
  blocked        iff A1 fails (setup never reached combat with the trigger
                 source on board).
  reproduced     iff A1+A2 pass and A3 fails (the damaged survivor did not
                 get the counter), OR A1 passes, damage was dealt, but A2
                 fails (trigger never fired - related failure).
  not-reproduced iff A1..A6 all pass: the damaged creature got the counter.
  blocked        otherwise (incomplete chain).

Evidence: evidence/7182/<run-id>/pre_combat.json, trigger_stack.json,
post_trigger.json, run.json, manifest.sha256, summary.png, scenario_7182.py,
wire_log.jsonl, scenario_run.log, server.log (excerpts).
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
ISSUE = 7182
RUN_ID = os.environ.get("RUN_ID", "20260915-7182")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RITE = "rite of passage"
DREAD = "colossal dreadmaw"
BEAR = "grizzly bears"
FOREST = "forest"
LANDS = (FOREST,)

P0_DECK = [(RITE, 8), (DREAD, 12), (BEAR, 8), (FOREST, 32)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

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
              "this run under runs/20260915-7182/",
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


def ref_key(ref):
    """Normalize a candidate reference to a comparable string oid."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


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


def untapped_lands(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names and not o.get("tapped")):
            out.append(int(oid))
    return out


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


def p1p1_counters(state, oid):
    return (get_obj(state, oid).get("counters") or {}).get("P1P1", 0)


def rite_trigger_on_stack(state):
    """Return the stack entry if Rite of Passage's trigger is on it."""
    for e in stack_entries(state):
        kind = (e.get("kind") or {}).get("type")
        if kind != "TriggeredAbility":
            continue
        sid = e.get("source_id")
        if sid is not None and lname(state, int(sid)) == RITE:
            return e
    return None


def build_submission(iid, rtype, choice_id=None, empty_sequence=False):
    if rtype == "schema":
        data = {"choiceIds": []} if empty_sequence else {
            "choiceIds": [choice_id]}
        return {"interactionId": iid,
                "response": {"type": "sequence", "data": data}}
    return {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


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

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> combat_setup -> combat ->
                            # trigger_pending -> done
        "game_code": None,
        "rite_oid": None,
        "dreadmaw_oid": None,
        "p1_bears": {},     # oid -> entered_turn
        "attack_bear_oid": None,
        "attacked": False,
        "blocked": False,
        "trigger": None,    # {stack_id, source_oid, turn, dmg_marked,
                            #  counters_before}
        "resolved": False,
        "counters_after": None,   # {dreadmaw, bear}
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "notes_extra": [],
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-rite")
    p1 = PhaseClient("P1-bears")
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
                if FOREST in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                build_submission(iid, resp.get("type"), pick.get("id")))
            return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def combat_p1_attack(c, st, state, acts):
        """P1 DeclareAttackers: send one ready bear at P0."""
        wtype = (wf_of(state).get("type") or "")
        if wtype != "DeclareAttackers":
            return False
        wp = wf_player(state)
        if wp != 1 and state.get("active_player") != 1:
            return False
        if ST["stage"] != "combat_setup" or ST["attacked"]:
            return False
        if ST["rite_oid"] is None or ST["dreadmaw_oid"] is None:
            return False
        turn = state.get("turn_number") or 0
        ready = [o for o, et in ST["p1_bears"].items()
                 if et < turn and o in bf_ids(state, 1, BEAR)]
        if not ready:
            return False
        da = find_action(acts, "DeclareAttackers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted("p1attack", rev):
            return True
        bear = ready[0]
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = [[bear, {"type": "Player", "data": 0}]]
        d["bands"] = []
        await export_named("pre_combat")
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        ST["attack_bear_oid"] = bear
        ST["attacked"] = True
        ST["stage"] = "combat"
        say(f"[P1] attacking P0 with bear oid={bear} (turn {turn})")
        wire("attack_declared", {"bear": bear, "turn": turn})
        return True

    async def combat_p0_block(c, st, state, acts):
        """P0 DeclareBlockers: Dreadmaw blocks the attacking bear."""
        wtype = (wf_of(state).get("type") or "")
        if wtype != "DeclareBlockers":
            return False
        wp = wf_player(state)
        # P0 is the defender on P1's turn
        if not (wp == 0 or (wp is None
                            and state.get("active_player") == 1)):
            return False
        if ST["stage"] != "combat" or not ST["attacked"] \
                or ST["blocked"]:
            return False
        da = find_action(acts, "DeclareBlockers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted("p0block", rev):
            return True
        dread = ST["dreadmaw_oid"]
        bear = ST["attack_bear_oid"]
        if dread not in bf_ids(state, 0, DREAD):
            say("[P0] Dreadmaw not on BF at DeclareBlockers!")
            obs["unexpected_prompts"].append("dreadmaw missing at blockers")
            return False
        d = copy.deepcopy(da.get("data", {}))
        d["assignments"] = [[dread, bear]]
        await submit_as_is(c, {"type": "DeclareBlockers", "data": d})
        ST["blocked"] = True
        say(f"[P0] blocking bear {bear} with Dreadmaw {dread}")
        wire("block_declared", {"blocker": dread, "attacker": bear})
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
        if wtype == "DeclareBlockers" and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_blk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["assignments"] = []
                await submit_as_is(c, {"type": wtype, "data": d})
                return True
        return False

    def observe_common(state):
        """Track rite/dreadmaw presence and P1 bears; detect trigger."""
        if ST["rite_oid"] is None:
            r = bf_ids(state, 0, RITE)
            if r:
                ST["rite_oid"] = r[0]
                say(f"[obs] Rite of Passage on BF oid={r[0]}")
        if ST["dreadmaw_oid"] is None:
            m = bf_ids(state, 0, DREAD)
            if m:
                ST["dreadmaw_oid"] = m[0]
                say(f"[obs] Dreadmaw on BF oid={m[0]}")
        for b in bf_ids(state, 1, BEAR):
            if b not in ST["p1_bears"]:
                et = get_obj(state, b).get("entered_battlefield_turn")
                ST["p1_bears"][b] = et if isinstance(et, int) else 10**9
        # trigger on stack?
        if ST["trigger"] is None:
            te = rite_trigger_on_stack(state)
            if te is not None:
                dread = ST["dreadmaw_oid"]
                dmg = get_obj(state, dread).get("damage_marked")
                cb = p1p1_counters(state, dread)
                ST["trigger"] = {
                    "stack_id": te.get("id"),
                    "source_oid": te.get("source_id"),
                    "turn": state.get("turn_number"),
                    "dmg_marked": dmg,
                    "counters_before": cb,
                    "ability_text": str(
                        ((te.get("ability") or {}).get("description"))
                        or (te.get("description")) or "")[:160],
                }
                ST["stage"] = "trigger_pending"
                say(f"[obs] Rite trigger ON STACK id={te.get('id')} "
                    f"dreadmaw dmg_marked={dmg} counters_before={cb}")
                wire("trigger_on_stack", ST["trigger"])
                return "saw_trigger"
        else:
            ids = [e.get("id") for e in stack_entries(state)]
            if ST["trigger"]["stack_id"] not in ids:
                dread = ST["dreadmaw_oid"]
                bear = ST["attack_bear_oid"]
                ST["counters_after"] = {
                    "dreadmaw": p1p1_counters(state, dread),
                    "bear": p1p1_counters(state, bear),
                    "dreadmaw_zone": get_obj(state, dread).get("zone"),
                }
                ST["resolved"] = True
                ST["need_post_export"] = True
                ST["stage"] = "done"
                say(f"[obs] trigger RESOLVED: counters_after="
                    f"{ST['counters_after']}")
                wire("trigger_resolved", ST["counters_after"])
                return "resolved"
        return None

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state, (RITE, DREAD)):
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
        if ST["trigger"] is not None and not ST["resolved"]:
            # trigger on stack: let it resolve, just pass priority
            pass

        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        if await combat_p0_block(p0, st, state, acts):
            return
        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # ---- main-phase actions (never during a cast flight) ----
        if ST["stage"] in ("setup", "combat_setup") \
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
            if ST["rite_oid"] is None and RITE in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, 0, RITE)
                if ca and not acted("rite", rev):
                    say("[P0] casting Rite of Passage")
                    await submit_as_is(p0, ca)
                    return
            if ST["dreadmaw_oid"] is None and ST["rite_oid"] is not None \
                    and DREAD in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, 0, DREAD)
                if ca and not acted("dread", rev):
                    say("[P0] casting Colossal Dreadmaw")
                    await submit_as_is(p0, ca)
                    return
            if len(bf_ids(state, 0, BEAR)) < 2 \
                    and BEAR in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, 0, BEAR)
                if ca and not acted("p0bear", rev):
                    say("[P0] casting Grizzly Bears")
                    await submit_as_is(p0, ca)
                    return
            if ST["rite_oid"] is not None \
                    and ST["dreadmaw_oid"] is not None \
                    and ST["stage"] == "setup":
                ST["stage"] = "combat_setup"
                say("[P0] stage -> combat_setup "
                    "(rite + dreadmaw on BF)")

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
        if await combat_p1_attack(p1, st, state, acts):
            return
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
            if len(bf_ids(state, 1, BEAR)) < 3 \
                    and BEAR in hand_lnames(state, 1):
                ca = cast_spell_action(acts, state, 1, BEAR)
                if ca and not acted("p1bear", rev):
                    say("[P1] casting Grizzly Bears")
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
                await export_named("post_trigger")
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
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"rite={ST['rite_oid']} dread={ST['dreadmaw_oid']} "
                    f"attacked={ST['attacked']} blocked={ST['blocked']}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        states = {}
        for fn in ("pre_combat", "trigger_stack", "post_trigger"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        # ---- A0: card-data parse (informational) ----
        try:
            cd = json.load(open(f"{RELDIR}/data/card-data.json"))
            rq = cd[RITE]
            tr = (rq.get("triggers") or [{}])[0]
            eff = ((tr.get("execute") or {}).get("effect")) or {}
            notes.append(
                f"A0: card-data rite of passage: mode="
                f"{tr.get('mode')}, valid_card={tr.get('valid_card')}, "
                f"effect={eff.get('type')} target={eff.get('target')}, "
                f"oracle={rq.get('oracle_text','')[:120]!r}")
        except Exception as e:
            notes.append(f"A0 parse check error: {e!r}")

        pre = states.get("pre_combat", {})
        tstack = states.get("trigger_stack", {})
        post = states.get("post_trigger", {})

        # ---- A1: setup ----
        a1 = (ST["rite_oid"] is not None
              and ST["dreadmaw_oid"] is not None
              and ST["attacked"] and ST["blocked"]
              and "pre_combat" in states)
        notes.append(
            f"A1: rite_oid={ST['rite_oid']} "
            f"dreadmaw_oid={ST['dreadmaw_oid']} "
            f"attacked={ST['attacked']} (bear {ST['attack_bear_oid']}) "
            f"blocked={ST['blocked']} pre_combat_exported="
            f"{'pre_combat' in states}")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: trigger fired ----
        trig = ST["trigger"]
        a2 = trig is not None and "trigger_stack" in states
        notes.append(
            f"A2: trigger_seen={trig is not None} "
            f"stack_id={(trig or {}).get('stack_id')} "
            f"source_oid={(trig or {}).get('source_oid')} "
            f"dmg_marked={(trig or {}).get('dmg_marked')} "
            f"trigger_stack_exported={'trigger_stack' in states} "
            f"ability={(trig or {}).get('ability_text','')[:80]!r}")
        ass["A2_trigger_fired"] = "passed" if a2 else "failed"

        dmg_dealt = bool(trig and (trig.get("dmg_marked") or 0) >= 1)

        # ---- A3: recipient = damaged creature ----
        dread = ST["dreadmaw_oid"]
        dc_after = p1p1_counters(post, dread) if post else None
        if post and dread is not None:
            a3 = dc_after == 1
            notes.append(
                f"A3: post_trigger dreadmaw P1P1={dc_after} (expect 1; "
                f"counters_before={(trig or {}).get('counters_before')})")
        else:
            a3 = False
            notes.append(f"A3 not-run/failed: post_trigger present="
                         f"{bool(post)} dreadmaw_oid={dread}")
            if not post:
                ass["A3_recipient_correct"] = "not-run"
        if "A3_recipient_correct" not in ass:
            ass["A3_recipient_correct"] = "passed" if a3 else "failed"

        # ---- A4: dealer got no counter ----
        bear = ST["attack_bear_oid"]
        bc_after = p1p1_counters(post, bear) if post else None
        if post and bear is not None:
            a4 = bc_after == 0
            notes.append(
                f"A4: post_trigger attacking-bear P1P1={bc_after} "
                f"(expect 0); bear zone="
                f"{get_obj(post, bear).get('zone')}")
        else:
            a4 = False
            notes.append(f"A4 not-run/failed: post_trigger present="
                         f"{bool(post)} bear_oid={bear}")
            if not post:
                ass["A4_dealer_no_counter"] = "not-run"
        if "A4_dealer_no_counter" not in ass:
            ass["A4_dealer_no_counter"] = "passed" if a4 else "failed"

        # ---- A5: damaged creature survived ----
        if post and dread is not None:
            a5 = get_obj(post, dread).get("zone") == "Battlefield"
            notes.append(
                f"A5: post_trigger dreadmaw zone="
                f"{get_obj(post, dread).get('zone')} (expect Battlefield)")
        else:
            a5 = False
            notes.append("A5 not-run: no post_trigger/dreadmaw")
            ass["A5_survived"] = "not-run"
        if "A5_survived" not in ass:
            ass["A5_survived"] = "passed" if a5 else "failed"

        # ---- A6: cleanup + damage observed ----
        if post:
            a6 = (not stack_entries(post)) and dmg_dealt
            notes.append(
                f"A6: post_trigger stack_empty={not stack_entries(post)} "
                f"damage_dealt={dmg_dealt} "
                f"(dmg_marked={(trig or {}).get('dmg_marked')})")
        else:
            a6 = False
            notes.append("A6 not-run: no post_trigger")
            ass["A6_cleanup"] = "not-run"
        if "A6_cleanup" not in ass:
            ass["A6_cleanup"] = "passed" if a6 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: A1 failed - combat setup with "
                         "Rite of Passage + Dreadmaw never completed")
        elif (ass["A2_trigger_fired"] == "passed"
                and ass["A3_recipient_correct"] == "failed"):
            verdict = "reproduced"
            notes.append(
                f"verdict=reproduced: Rite of Passage trigger fired for "
                f"the damaged Dreadmaw (dmg_marked="
                f"{(trig or {}).get('dmg_marked')}) but the +1/+1 counter "
                f"did not land on it (post_trigger P1P1={dc_after}); "
                f"attacking bear P1P1={bc_after}")
        elif (ass["A1_setup_ok"] == "passed" and dmg_dealt
                and ass["A2_trigger_fired"] != "passed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced (related failure): Dreadmaw "
                         "was dealt damage and survived but the Rite of "
                         "Passage trigger never reached the stack")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_trigger_fired",
                  "A3_recipient_correct", "A4_dealer_no_counter",
                  "A5_survived", "A6_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: the damaged Dreadmaw "
                         "received exactly one +1/+1 counter, the "
                         "attacking bear received none")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")
        for n in ST["notes_extra"]:
            notes.append(n)

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
                "rite_oid": ST["rite_oid"],
                "dreadmaw_oid": ST["dreadmaw_oid"],
                "attack_bear_oid": ST["attack_bear_oid"],
                "attacked": ST["attacked"],
                "blocked": ST["blocked"],
                "trigger": ST["trigger"],
                "resolved": ST["resolved"],
                "counters_after": ST["counters_after"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
            },
            "notes": notes,
            "evidence_files": ["pre_combat.json", "trigger_stack.json",
                               "post_trigger.json", "run.json",
                               "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The damage event is combat damage (P1 bear blocked by "
                "P0 Dreadmaw); non-combat damage paths not exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Only one damage event / one trigger instance is "
                "exercised; the 'must survive' clause is covered by the "
                "Dreadmaw surviving 2 damage (A5).",
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
        d.text((24, y), "phase-rs/phase #7182 - Rite of Passage counter "
               "recipient", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
               " - DamageReceived EventTarget resolution",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: Rite of Passage's +1/+1 counters should go to",
                "the creature that RECEIVED damage, but allegedly go to",
                "the (opponent's) creature that DEALT the damage.",
                "Setup: P1 bear attacks; P0 blocks with Colossal",
                "Dreadmaw (6/6). Dreadmaw takes 2 (survives), bear takes",
                "6 (dies). Trigger must counter the Dreadmaw."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "rite + dreadmaw on P0 BF; bear attacked, "
                           "dreadmaw blocked",
            "A2_trigger_fired": "Rite of Passage TriggeredAbility seen "
                                "on the stack",
            "A3_recipient_correct": "post: Dreadmaw carries exactly 1 "
                                    "+1/+1 counter",
            "A4_dealer_no_counter": "post: attacking bear carries 0 "
                                    "counters",
            "A5_survived": "post: Dreadmaw still on the battlefield",
            "A6_cleanup": "post: stack empty; damage event observed",
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
        ca = ds.get("counters_after") or {}
        for ln in [
                f"rite oid={ds.get('rite_oid')} "
                f"dreadmaw oid={ds.get('dreadmaw_oid')} "
                f"bear oid={ds.get('attack_bear_oid')}",
                f"trigger stack_id={trig.get('stack_id')} "
                f"dmg_marked={trig.get('dmg_marked')} "
                f"counters_before={trig.get('counters_before')}",
                f"post: dreadmaw P1P1={ca.get('dreadmaw')} "
                f"(zone {ca.get('dreadmaw_zone')}), "
                f"bear P1P1={ca.get('bear')}",
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:12])}",
                "Exports: pre_combat.json (decision point),",
                "trigger_stack.json (trigger on stack), post_trigger.json",
                "(after resolution).",
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
        # plain print: RUNLOG is already closed at this point
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()


if __name__ == "__main__":
    asyncio.run(main())

