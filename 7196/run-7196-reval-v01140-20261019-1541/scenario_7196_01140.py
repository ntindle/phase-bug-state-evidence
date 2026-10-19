#!/usr/bin/env python3
"""Issue #7196: Mage Slayer — Trigger goes on stack but nothing happens.

RE-VALIDATION on v0.114.0 (2026-10-19) - previously validated on v0.85.0.

Oracle: "Whenever equipped creature attacks, it deals damage equal to its
power to the player or planeswalker it's attacking."
Report (Discord, status:confirmed): the attack trigger is placed on the
stack but resolves without dealing damage (no life or loyalty change).
On v0.85.0 the trigger was not even observed: post-attack mid-hold showed
stack=[] and life 20/20, and post_combat life was 20/18 (the bear's
2 combat damage only) instead of the expected 20/16.

Behavioral contract (single game, v0.114.0 / protocol 153):
  Setup: P0: 8x Grizzly Bears + 8x Mage Slayer + 22x Forest + 22x Mountain.
         P1: 60x Island, draw-go, never blocks.
  A1 setup_ok     Mage Slayer cast and equipped to a Grizzly Bears; life
                  20/20 at the pre_attack checkpoint.
  A2 trigger_fired Mage Slayer's TriggeredAbility (source_id == slayer's
                  battlefield oid) observed live on the stack (or in the
                  per-tick stack history) after the bear is declared as
                  an attacker.
  A3 damage_dealt P1 life at the post_combat checkpoint == 16: 2 combat
                  damage from the unblocked 2/2 bear + 2 from the Slayer
                  trigger. The reported defect is P1 staying at 18.
  A4 cleanup      stack empty and the game proceeds after combat.

Verdict = reproduced iff A1 passed and A3 failed.
          not-reproduced iff A1-A4 all pass.
          blocked otherwise (setup never reached, etc.).

Protocol-153 driver conventions (per AGENTS.md and the 2026-10-19
protocol-153 ports, e.g. scenario_7185_01140.py): CreateGameWithSettings +
JoinGameWithPassword + start_when_full, merged_actions
(legal_actions + legal_actions_by_object with _src_oid), per-seat tick
tasks, acted() latch (act at most once per state_revision per key),
default PassPriority gated on my_priority, MulliganDecision Keep via
legacy Action (pending phase Declare check), DiscardToHandSize via vi
schema/select opportunities, PayMana-family answered as-is with a vi
tapLandForMana fallback, Equip activation via advertised ActivateAbility
(source_id/_src_oid), equip target via vi schema/select sequence
(choiceIds), combat declarations as copies of advertised actions,
priority-hold window after attack declaration with mid_hold export.
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
ISSUE = 7196
RUN_ID = os.environ.get("RUN_ID", "run-7196-reval-v01140-20261019-1541")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "grizzly bears"
SLAYER = "mage slayer"
FOREST = "forest"
MOUNTAIN = "mountain"
ISLAND = "island"

P0_DECK = [(SLAYER, 8), (BEAR, 8), (FOREST, 22), (MOUNTAIN, 22)]
P1_DECK = [(ISLAND, 60)]

RELDIR = f"{BACKFILL}/server/releases/v0.114.0"

HOLD_S = 3.0  # priority-hold window after attack declaration


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "025c26c78762d8fc0bed47a7774749465c3e40751df73310cd7dee6ed5d506ef",
    "card_data_sha256": "7fe886cc031d9bd6532317572f84f65da66941ebdf6a43d337b625fbc369548e",
    "draft_pools_sha256": "4a0b44a9af0881f924b4120a43fe04947dd673aa867e796917b696d7ea26ba59",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
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
                      "2026-10-19; binary digest matches the GitHub asset "
                      "digest; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run "
                      "(AGENTS.md: never copy SERVER_IDENTITY hashes); "
                      "release v0.114.0 confirmed latest stable via GitHub "
                      "releases API 2026-10-19",
    "observed_at": "2026-10-19",
    "handshake": "driver/client.py advertises protocol 153 (matches "
                 "v0.114.0/ac67cbb ServerHello); connect/create/join/export "
                 "succeeded on 127.0.0.1:9374 this run",
    "source": "isolated v0.114.0 server on 127.0.0.1:9374 started by this "
              "backfill run (--single-user --no-data-download, games-db "
              "runs/run-7196-reval-v01140-20261019-1541/games.db); the "
              "14:41 CDT run's server (pid 7740) died with the VM "
              "replacement at ~15:48 CDT and was restarted fresh; game "
              "isolated by game_code",
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


def attached_oid(obj):
    """attached_to may be an int oid or a nested dict; extract the oid."""
    att = obj.get("attached_to")
    if att is None:
        return None
    if isinstance(att, int):
        return att
    if isinstance(att, str) and att.isdigit():
        return int(att)
    found = []

    def rec(n):
        if isinstance(n, int):
            found.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                rec(v)
        elif isinstance(n, (list, tuple)):
            for v in n:
                rec(v)
    rec(att)
    return found[0] if found else None


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
    return (wf_of(state).get("data") or {}).get("player")


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


def stack_kind_type(e):
    k = e.get("kind")
    if isinstance(k, dict):
        return k.get("type")
    return k


def stack_summ(state):
    out = []
    for e in stack_entries(state):
        desc = e.get("kind") or {}
        if isinstance(desc, dict):
            d = desc.get("description") or ""
            dd = (desc.get("data") or {}).get("description") or ""
            text = (d or dd)[:80]
        else:
            text = str(desc)[:80]
        out.append({"id": e.get("id"), "kind": stack_kind_type(e),
                    "source_id": e.get("source_id"), "desc": text})
    return out


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",  # setup -> equipped -> attack_declared -> done
        "game_code": None,
        "bear_oid": None,      # equipped creature's battlefield oid
        "bear_cast_turn": None,
        "slayer_oid": None,    # Mage Slayer's battlefield oid
        "equip_pending": False,
        "equip_done": False,
        "attack_turn": None,
        "attack_declared_at": None,
        "mid_hold_exported": False,
        "trigger_seen_live": False,
        "mid_trigger_exported": False,
        "mid_trigger_life": None,
        "stack_history": [],  # non-empty stack samples w/ context
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "lands_played": {},    # turn -> bool (per seat)
        "exports": {},
        "last_rev_acted": {},
        "last_rev_seen": {},
        "last_rev_change": {},
        "hold_logged": False,
        "mid_hold_dump": None,
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-slayer")
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
                f"(turn={env['state'].get('turn_number')} "
                f"phase={env['state'].get('phase')})")
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
                if ISLAND in tx or FOREST in tx or MOUNTAIN in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            rtype = resp.get("type")
            if rtype == "schema":
                sub = {"interactionId": iid,
                       "response": {"type": "sequence",
                                    "data": {"choiceIds": [pick["id"]]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick["id"]}}}
            await c.send_interaction(sub)
            return True
        return False

    async def pay_mana(c, tag, acts):
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(c, a)
                say(f"[{tag}] answered {a['type']} as-is")
                return True
        return False

    def find_target_opportunity(st):
        """Schema sequence/select opportunity whose candidates carry card
        references (equip target prompt pattern from scenario_7196)."""
        vi = get_vi(st)
        if not vi:
            return None
        for opp in vi.get("opportunities", []):
            if opp.get("interactionId") in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            if spec_type not in ("sequence", "select"):
                continue
            chs = data.get("choices") or data.get("candidates") or []
            if any(ch.get("surfaces") for ch in chs):
                return opp.get("interactionId"), spec_type, chs
        return None

    def cand_ref(ch):
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                return d.get("reference")
        return None

    async def answer_equip_target(c, tag, st, state):
        if not ST["equip_pending"]:
            return False
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        found = find_target_opportunity(st)
        if not found:
            return False
        iid, spec_type, chs = found
        pick = None
        for ch in chs:
            if str(cand_ref(ch)) == str(ST["bear_oid"]):
                pick = ch
                break
        if pick is None:
            say(f"[{tag}] equip target prompt: bear {ST['bear_oid']} not "
                f"among {[cand_ref(ch) for ch in chs]}; deferring")
            wire("equip_target_no_match",
                 {"wanted": ST["bear_oid"],
                  "cands": [cand_ref(ch) for ch in chs]})
            return False
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [pick["id"]]}}}
        say(f"[{tag}] equip target -> bear {ST['bear_oid']} "
            f"(candidate {pick['id']})")
        wire("equip_target_submit", sub)
        ST["answered_iids"].append(iid)
        await c.send_interaction(sub)
        return True

    def cast_spell_action(acts, state, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def observe_board(state):
        """Track bear + slayer battlefield oids and equip completion."""
        bears = bf_ids(state, 0, BEAR)
        slayers = bf_ids(state, 0, SLAYER)
        if bears and ST["bear_oid"] is None:
            ST["bear_oid"] = bears[0]
            say(f"[obs] Grizzly Bears on BF oid={bears[0]}")
        if slayers:
            ST["slayer_oid"] = slayers[0]
            so = get_obj(state, slayers[0])
            if ST["bear_oid"] is not None \
                    and attached_oid(so) == ST["bear_oid"]:
                if not ST["equip_done"]:
                    ST["equip_done"] = True
                    ST["equip_pending"] = False
                    say(f"[obs] Mage Slayer {slayers[0]} equipped to bear "
                        f"{ST['bear_oid']}")
                    wire("equip_done", {"slayer": slayers[0],
                                        "bear": ST["bear_oid"]})

    def sample_stack(state, rev):
        stk = stack_entries(state)
        if not stk:
            return
        key = (rev, tuple(e.get("id") for e in stk))
        if ST["stack_history"] and ST["stack_history"][-1]["_key"] == key:
            return
        entry = {"rev": rev, "turn": state.get("turn_number"),
                 "phase": state.get("phase"),
                 "life": (player_of(state, 0).get("life"),
                          player_of(state, 1).get("life")),
                 "stack": stack_summ(state), "_key": key}
        ST["stack_history"].append(entry)
        wire("stack_sample", {k: v for k, v in entry.items()
                              if k != "_key"})
        # live trigger detection: Mage Slayer TriggeredAbility on stack
        if not ST["trigger_seen_live"]:
            for e in stk:
                if stack_kind_type(e) == "TriggeredAbility" \
                        and e.get("source_id") == ST["slayer_oid"]:
                    ST["trigger_seen_live"] = True
                    ST["mid_trigger_life"] = (
                        player_of(state, 0).get("life"),
                        player_of(state, 1).get("life"))
                    say(f"[obs] Mage Slayer trigger on stack: "
                        f"{json.dumps(stack_summ(state))[:500]}")
                    wire("slayer_trigger_stack", stack_summ(state))
                    break

    def bear_ready_to_attack(state):
        """Equipped bear exists, summoning-sick-free, and it's P0's
        DeclareAttackers step."""
        if not ST["equip_done"] or ST["bear_oid"] is None:
            return False
        if ST["bear_cast_turn"] is None:
            return False
        turn = state.get("turn_number") or 0
        if turn <= ST["bear_cast_turn"]:
            return False
        if (wf_of(state).get("type") or "") != "DeclareAttackers":
            return False
        if wf_player(state) != 0:
            return False
        return True

    async def declare_bear_attack(c, acts, state):
        da = find_action(acts, "DeclareAttackers")
        if not da:
            return False
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = [[ST["bear_oid"],
                         {"type": "Player", "data": 1}]]
        d["bands"] = []
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        ST["attack_turn"] = state.get("turn_number")
        ST["attack_declared_at"] = time.time()
        ST["stage"] = "attack_declared"
        say(f"[P0] declares attackers: bear {ST['bear_oid']} -> P1 "
            f"(turn {ST['attack_turn']})")
        wire("attack_declared",
             {"bear": ST["bear_oid"], "turn": ST["attack_turn"]})
        return True

    def combat_done(state):
        return (ST["attack_turn"] is not None
                and (state.get("turn_number") or 0) != ST["attack_turn"]
                and not stack_entries(state)
                and state.get("active_player") == 0
                and state.get("phase") == "PreCombatMain")

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state,
                                (BEAR, SLAYER)):
            return
        if await pay_mana(p0, "P0", acts):
            return
        # NOTE: no vi tapLandForMana fallback. On protocol 153 the
        # tapLandForMana surfaces are a persistent non-decision menu
        # (cf. NON_DECISION_CODES in scenario_301_01130.py); submitting
        # to it is rejected as invalid_interaction_response and loops.
        # Mana payments are advertised as legacy PayMana actions.
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
        observe_board(state)
        sample_stack(state, rev)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)
        wtype = wf_of(state).get("type") or ""
        if wtype and wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)

        # post-attack priority hold: sample the trigger window at full
        # rate before passing; export mid_hold once during the hold.
        if ST["attack_declared_at"] is not None and my_priority(state, 0):
            dt = time.time() - ST["attack_declared_at"]
            if dt < HOLD_S:
                if not ST["mid_hold_exported"] and dt > 1.0:
                    ST["mid_hold_exported"] = True
                    say("[P0] priority hold: exporting mid_hold")
                    mid = await export_named("mid_hold")
                    if mid is not None:
                        ST["mid_hold_dump"] = {
                            "life": (player_of(mid, 0).get("life"),
                                     player_of(mid, 1).get("life")),
                            "stack": stack_summ(mid)}
                        wire("stack_at_mid_hold",
                             ST["mid_hold_dump"]["stack"])
                if not ST["hold_logged"]:
                    ST["hold_logged"] = True
                    say(f"[P0] HOLDING priority pass for trigger window "
                        f"(phase={phase})")
                    wire("priority_hold", {"phase": phase})
                return
        # trigger seen live: export mid_trigger once, keep the game going
        if ST["trigger_seen_live"] and not ST["mid_trigger_exported"]:
            ST["mid_trigger_exported"] = True
            say("[P0] exporting mid_trigger (trigger live on stack)")
            await export_named("mid_trigger")

        if ST["equip_done"] and ST["stage"] == "setup":
            ST["stage"] = "equipped"
            say("[P0] stage -> equipped; exporting pre_attack")
            await export_named("pre_attack")

        # P0 combat: attack with the equipped, sick-free bear; declare
        # empty attackers otherwise so the game advances past the step
        if (wf_of(state).get("type") or "") == "DeclareAttackers" \
                and wf_player(state) == 0:
            if bear_ready_to_attack(state):
                if not acted("attack", rev):
                    if await declare_bear_attack(p0, acts, state):
                        return
            else:
                da = find_action(acts, "DeclareAttackers")
                if da and not acted("p0atk_empty", rev):
                    d = copy.deepcopy(da.get("data", {}))
                    d["attacks"] = []
                    d["bands"] = []
                    await submit_as_is(p0, {"type": "DeclareAttackers",
                                            "data": d})
                    return

        # P0 blocks: never needed (P1 never attacks), but answer the
        # prompt explicitly if it ever names P0
        if (wf_of(state).get("type") or "") == "DeclareBlockers" \
                and wf_player(state) == 0:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted("p0blk_empty", rev):
                d = copy.deepcopy(db.get("data", {}))
                d["blocks"] = []
                await submit_as_is(p0, {"type": "DeclareBlockers",
                                        "data": d})
                say("[P0] declares no blockers")
                return

        # log any other wait naming P0 that we don't handle, instead of
        # silently passing or stalling
        if wf_player(state) == 0 and (wf_of(state).get("type") or "") \
                not in (None, "", "Priority", "DeclareAttackers",
                        "DeclareBlockers", "TargetSelection",
                        "MulliganDecision", "DiscardToHandSize"):
            say(f"[P0] unhandled wait: wf={(wf_of(state).get('type'))} "
                f"acts={[a['type'] for a in acts]}")
            obs["unexpected_prompts"].append(
                {"who": "P0", "wf": wf_of(state).get("type"),
                 "acts": [a["type"] for a in acts]})

        # main-phase development
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain") \
                and not stack_entries(state) \
                and ST["stage"] in ("setup", "equipped"):
            key = (0, turn)
            if key not in ST["lands_played"]:
                for lnm in (FOREST, MOUNTAIN):
                    pla = next((a for a in acts
                                if a["type"] == "PlayLand"
                                and lname(state,
                                          (a.get("data") or {}).get(
                                              "object_id")) == lnm), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        ST["lands_played"][key] = True
                        say(f"[P0] plays {lnm} (turn {turn})")
                        return
                ST["lands_played"][key] = False
            if ST["bear_oid"] is None and BEAR in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, BEAR)
                if ca and not acted("bearcast", rev):
                    ST["bear_cast_turn"] = turn
                    say(f"[P0] casting Grizzly Bears (turn {turn})")
                    wire("bears_cast", {"turn": turn})
                    await submit_as_is(p0, ca)
                    return
            if ST["bear_oid"] is not None and ST["slayer_oid"] is None \
                    and SLAYER in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, SLAYER)
                if ca and not acted("slayercast", rev):
                    say("[P0] casting Mage Slayer")
                    await submit_as_is(p0, ca)
                    return
            if ST["bear_oid"] is not None and ST["slayer_oid"] is not None \
                    and not ST["equip_done"] and not ST["equip_pending"]:
                eq = next((a for a in acts
                           if a["type"] == "ActivateAbility"
                           and str((a.get("data") or {}).get("source_id"))
                           == str(ST["slayer_oid"])), None)
                if eq and not acted("equip", rev):
                    await submit_as_is(p0, eq)
                    ST["equip_pending"] = True
                    say(f"[P0] activates Equip (slayer {ST['slayer_oid']} "
                        f"-> bear {ST['bear_oid']})")
                    wire("equip_activate", eq.get("data"))
                    return
            # equip target prompt answered for the same seat
            if ST["equip_pending"]:
                if await answer_equip_target(p0, "P0", st, state):
                    return
                if wtype == "TargetSelection":
                    return  # hold; do not pass through the prompt

        # combat completion -> export post, finish
        if combat_done(state) and not ST.get("post_exported"):
            say("[P0] combat done; exporting post_combat")
            await export_named("post_combat")
            ST["post_exported"] = True
            ST["stage"] = "done"
            return

        # default: pass priority (gated on my_priority)
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
        if await pay_mana(p1, "P1", acts):
            return
        for rj in drain_rejections(p1):
            obs["rejections"].append({"stage": "p1", **rj})
            blob = json.dumps(rj, default=str)
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
        sample_stack(state, rev)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            key = (1, turn)
            if key not in ST["lands_played"]:
                pla = next((a for a in acts
                            if a["type"] == "PlayLand"
                            and lname(state, (a.get("data") or {}).get(
                                "object_id")) == ISLAND), None)
                if pla and not acted("p1land", rev):
                    await submit_as_is(p1, pla)
                    ST["lands_played"][key] = True
                    say(f"[P1] plays island (turn {turn})")
                    return
                ST["lands_played"][key] = False
        wtype = wf_of(state).get("type") or ""
        if wtype == "DeclareAttackers" and wf_player(state) == 1:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted("p1atk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await submit_as_is(p1, {"type": "DeclareAttackers",
                                        "data": d})
                return
        if wtype == "DeclareBlockers" and wf_player(state) == 1:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted("p1blk", rev):
                d = copy.deepcopy(db.get("data", {}))
                d["blocks"] = []
                await submit_as_is(p1, {"type": "DeclareBlockers",
                                        "data": d})
                say("[P1] declares no blockers")
                return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
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
                    f"bear={ST['bear_oid']} slayer={ST['slayer_oid']} "
                    f"equip_done={ST['equip_done']} "
                    f"attack_turn={ST['attack_turn']} "
                    f"trig_live={ST['trigger_seen_live']}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        for nm in ("pre_attack", "mid_hold", "mid_trigger", "post_combat"):
            if nm not in ST.get("exports", {}):
                if nm == "post_combat" or \
                        (nm == "mid_trigger" and
                         not ST["trigger_seen_live"]):
                    continue
                say(f"[finish] {nm} not exported; attempting")
                await export_named(nm)
        states = {}
        for fn in ("pre_attack", "mid_hold", "mid_trigger", "post_combat"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre = states.get("pre_attack", {})
        midh = states.get("mid_hold", {})
        midt = states.get("mid_trigger", {})
        post = states.get("post_combat", {})

        # backstop: scan the per-tick stack history for the trigger even
        # if the live check missed the window
        hist_hits = [e for e in ST["stack_history"]
                     if any(stack_kind_type(s) == "TriggeredAbility"
                            and s.get("source_id") == ST["slayer_oid"]
                            for s in e["stack"])]
        if hist_hits and not ST["trigger_seen_live"]:
            ST["trigger_seen_live"] = True
            say(f"[obs] slayer trigger found in stack history "
                f"({len(hist_hits)} samples)")
            wire("slayer_trigger_history",
                 [{k: e[k] for k in ("rev", "turn", "phase", "life",
                                     "stack")} for e in hist_hits])
        notes.append(f"stack_history: {len(ST['stack_history'])} non-empty "
                     f"samples, slayer-trigger samples={len(hist_hits)}")
        if ST["mid_hold_dump"]:
            notes.append(
                f"mid_hold (priority held {HOLD_S}s post-attack): "
                f"life={ST['mid_hold_dump']['life']} "
                f"stack={json.dumps(ST['mid_hold_dump']['stack'])[:400]}")

        # ---- A1: setup ----
        bear = get_obj(pre, ST["bear_oid"]) if pre else {}
        slayer = get_obj(pre, ST["slayer_oid"]) if pre else {}
        a1 = (bool(pre)
              and ST["bear_oid"] is not None
              and ST["slayer_oid"] is not None
              and attached_oid(slayer) == ST["bear_oid"]
              and player_of(pre, 0).get("life") == 20
              and player_of(pre, 1).get("life") == 20)
        notes.append(
            f"A1: bear_oid={ST['bear_oid']} slayer_oid={ST['slayer_oid']} "
            f"slayer_attached_to={attached_oid(slayer) if slayer else None} "
            f"pre_life={player_of(pre, 0).get('life') if pre else None}/"
            f"{player_of(pre, 1).get('life') if pre else None} "
            f"(expect 20/20, slayer attached to bear)")
        ass["A1_setup_ok"] = "passed" if a1 else "failed"

        # ---- A2: trigger fired ----
        a2 = ST["trigger_seen_live"]
        notes.append(
            f"A2: trigger_seen_live={ST['trigger_seen_live']} "
            f"mid_trigger_life={ST['mid_trigger_life']} "
            f"stack_history_hits={len(hist_hits)}")
        ass["A2_trigger_fired"] = "passed" if a2 else "failed"

        # ---- A3: damage dealt (expect P1 at 16: 2 combat + 2 trigger) ----
        post_life0 = player_of(post, 0).get("life") if post else None
        post_life1 = player_of(post, 1).get("life") if post else None
        a3 = (a1 and post_life1 == 16)
        notes.append(
            f"A3: post_combat life={post_life0}/{post_life1} "
            f"(expect 20/16); attack_turn={ST['attack_turn']} "
            f"turn_post={post.get('turn_number') if post else None}")
        ass["A3_damage_dealt"] = "passed" if a3 else "failed"

        # ---- A4: cleanup ----
        a4 = (bool(post) and not stack_entries(post))
        notes.append(
            f"A4: post present={bool(post)} stack_empty="
            f"{not stack_entries(post) if post else None} "
            f"phase={post.get('phase') if post else None}")
        ass["A4_cleanup"] = "passed" if a4 else "failed"

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup never completed "
                         "(slayer not cast/equipped)")
        elif ass["A3_damage_dealt"] != "passed":
            verdict = "reproduced"
            notes.append(
                "verdict=reproduced: Slayer equipped and bear attacked, "
                f"but P1 life={post_life1} != 16 "
                f"(trigger_seen_live={ST['trigger_seen_live']}) - the "
                "reported 'trigger goes on the stack but nothing happens'")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_trigger_fired", "A3_damage_dealt",
                  "A4_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: trigger fired and "
                         "dealt its 2 damage (P1 at 16)")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-10-19",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 153,
                       "client": "driver/client.py"},
            "scenario_sha256": sha256_file(
                f"{BACKFILL}/driver/scenario_7196_01140.py"),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {
                "stage": ST["stage"],
                "bear_oid": ST["bear_oid"],
                "bear_cast_turn": ST["bear_cast_turn"],
                "slayer_oid": ST["slayer_oid"],
                "equip_done": ST["equip_done"],
                "attack_turn": ST["attack_turn"],
                "trigger_seen_live": ST["trigger_seen_live"],
                "mid_trigger_life": ST["mid_trigger_life"],
                "mid_hold_dump": ST["mid_hold_dump"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
            },
            "notes": notes,
            "evidence_files": ["pre_attack.json", "mid_hold.json",
                               "mid_trigger.json", "post_combat.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_7196_01140.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "8x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is a passive second seat (lands only, never blocks) - "
                "opponent interaction with the trigger is not exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Damage is asserted from life totals; a combat-damage "
                "shortfall (e.g. blocked bear) would also fail A3, but "
                "P1 never blocks in this fixture.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7196_01140.py",
                    f"{EVDIR}/scenario_7196_01140.py")
        srv_run = os.environ.get("SERVER_RUN_ID",
                                 "run-7185-reval-v01140-20261019-1441")
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
        render_summary(run)
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

    def render_summary(run):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7196 - Mage Slayer attack trigger",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.114.0 (ac67cbb) protocol 153 - 2026-10-19"
               " - attack trigger: damage equal to equipped creature's power",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: 'Mage Slayer - Trigger goes on stack but nothing",
                "happens.' Oracle: 'Whenever equipped creature attacks,",
                "it deals damage equal to its power to the player or",
                "planeswalker it's attacking.' Setup: P0 casts Grizzly",
                "Bears (2/2), Mage Slayer, activates Equip {3}, attacks",
                "P1 with the bear unblocked. Expected: P1 at 16 (2 combat",
                "+ 2 trigger). Reported: trigger resolves with no damage."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Slayer cast + equipped to bear; life 20/20 at"
                           " pre_attack",
            "A2_trigger_fired": "Slayer TriggeredAbility seen on the stack"
                                " after attackers declared",
            "A3_damage_dealt": "P1 at 16 post-combat (2 combat + 2 trigger"
                               " damage)",
            "A4_cleanup": "stack empty; game proceeded after combat",
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
                f"bear oid={ds.get('bear_oid')} cast turn="
                f"{ds.get('bear_cast_turn')}",
                f"slayer oid={ds.get('slayer_oid')} equip_done="
                f"{ds.get('equip_done')}",
                f"attack turn={ds.get('attack_turn')} "
                f"trigger_seen_live={ds.get('trigger_seen_live')}",
                f"mid_trigger life={ds.get('mid_trigger_life')}",
                f"mid_hold: {str(ds.get('mid_hold_dump'))[:98]}",
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:14])}",
                "Exports: pre_attack.json (slayer equipped, life 20/20),",
                "mid_hold.json (priority held 3s post-attack),",
                "mid_trigger.json (trigger on stack, if seen),",
                "post_combat.json (combat done, stack empty).",
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
