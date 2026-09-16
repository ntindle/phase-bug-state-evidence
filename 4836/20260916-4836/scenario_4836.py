#!/usr/bin/env python3
"""Issue #4836: The Mindskinner taps on attack but does not mill.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-07-01, triage-confirmed, area:engine/area:parser,
mechanic:replacement-effects): "The Mindskinner taps on attack, but its
damage-prevention/mill replacement does not mill cards."

Oracle (verified against pinned v0.84.0 card-data.json, key 'the mindskinner'):
  "The Mindskinner can't be blocked.
   If a source you control would deal damage to an opponent, prevent that
   damage and each opponent mills that many cards."
  10/1, {U}{U}{U}. Parsed in the dataset as a DamageDone replacement with
  Prevention(All) shield + Mill { count: Ref(EventContextAmount),
  target: Controller, player_scope: Opponent } -- so the parse looks
  complete; any failure is engine-side. (A 2026-08-02 reopen note adds the
  non-combat branch and an Undead Alchemist interaction; Alchemist is not
  driven here -- only the mill events themselves.)

This is a RE-VALIDATION run: issue #4836 was validated not-reproduced on
v0.78.0 (run 20260909-4836, evidence commit
33d3b9c72ab260278a88925f5e07098dbb0a429d, maintained comment
5608938191) but the ledger entry was lost in a VM replacement, and the
release pin has since advanced to v0.84.0. The v0.78.0 result is stale per
the playbook; this run re-drives the same contract on the pinned release
(protocol 71).

Expected:
  E1: The Mindskinner attacks as a 10/1 and taps as an attacker.
  E2: Its 10 combat damage to the defending opponent is prevented: the
      defending player's life is unchanged (20 -> 20).
  E3: EACH opponent mills 10 (the prevented amount): library -10 / graveyard
      +10 for both P1 and P2 (3-seat game so "each opponent" is observable).
  E4: A non-combat source P0 controls (Lightning Bolt, 3 damage) is likewise
      prevented: defending player life unchanged, each opponent mills 3.

Assertions:
  A1_setup_ok       P0 DeclareAttackers with an attack-ready Mindskinner;
                    pre.json exported; P1 life 20.
  A2_attack_taps    declared Mindskinner is tapped=true and listed in
                    state.combat.attackers (the reported "taps on attack").
  A3_damage_prevented  P1 life unchanged 20 -> 20 across combat.
  A4_mill_combat    P1 and P2 each mill exactly 10 (lib -10, gy +10).
  A5_bolt_branch    Lightning Bolt at P1: life unchanged, P1 and P2 each
                    mill exactly 3 (lib -3, gy +3).
  A6_cleanup        post.json: stack empty, game proceeding.

Verdict rule: A3 failing (damage went through) or A4 failing (no mill after
the attack) -> reproduced on v0.84.0 (the reported symptom). A5 failing ->
reproduced for the non-combat branch. All of A2-A5 passing -> not-reproduced
(scoped to v0.84.0; the report's original build is not tested here; not a
fix claim).

Evidence: evidence/4836/<run-id>/pre.json (P0 DeclareAttackers),
mid_attack.json (attackers declared), mid_combat.json (P0 PostCombatMain
after combat), mid_bolt.json (after Bolt resolution), post.json, run.json,
manifest.sha256, summary.png, scenario_4836.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
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
ISSUE = 4836
RUN_ID = os.environ.get("RUN_ID", "20260916-4836")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.json")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MIND = "the mindskinner"
BOLT = "lightning bolt"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
PLAINS = "plains"

P0_DECK = [("The Mindskinner", 8), ("Lightning Bolt", 12),
           ("Island", 22), ("Mountain", 18)]
P1_DECK = [("Forest", 60)]
P2_DECK = [("Plains", 60)]

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
    "mode": "Full",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "at pin time 2026-09-15; data digests match the signed "
                      "manifest; digests recomputed against on-disk files "
                      "this run; release v0.84.0 confirmed latest stable via "
                      "GitHub releases API 2026-09-16",
    "observed_at": "2026-09-16",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/20260916-4836/",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def obj_name(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?")


def lname(state, oid):
    return obj_name(state, oid).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []) or [])


def gy_count(state, pid):
    return len(player_of(state, pid).get("graveyard", []) or [])


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", []) or []]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid, name):
    return [o for o in bf_ids(state, pid, name)
            if not get_obj(state, o).get("tapped")]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    return not o.get("tapped") and not o.get("summoning_sick")


def attacker_oids(state):
    out = set()
    for a in (state.get("combat") or {}).get("attackers") or []:
        if isinstance(a, dict):
            for k in ("attacker", "attacker_id", "object_id", "id"):
                if k in a:
                    try:
                        out.add(int(a[k]))
                    except Exception:
                        pass
        else:
            try:
                out.add(int(a[0] if isinstance(a, (list, tuple)) else a))
            except Exception:
                pass
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
    return (wf_of(state).get("data") or {}).get("player")


def my_priority(state, pid):
    return wf_of(state).get("type") == "Priority" \
        and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


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
        "stage": "setup",
        "finished": False,
        "game_code": None,
        "answered_iids": [],
        "mulls": {},
        "bottomed": {},
        "mind_cast": False,
        "bolt_cast": False,
        "bolt_done": False,
        "attack_turn": None,
        "mindskinner_oid": None,
        "pre_exported": False,
        "mid_attack_exported": False,
        "mid_combat_exported": False,
        "mid_bolt_exported": False,
        "post_exported": False,
        "pending_target": None,   # {"kind": "player", "seat": 1} while the
                                  # Bolt's target prompt may be outstanding
        "cast_awaiting": None,    # {"name","turn","want"} while a cast's
                                  # target prompt may still be pending
        "target_prompts_seen": 0,
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
    }
    obs = {
        "rejections": [], "tick_errors": [],
        "life_pre": None, "lib_pre": {}, "gy_pre": {},
        "life_midc": None, "lib_midc": {}, "gy_midc": {},
        "life_midb": None, "lib_midb": {}, "gy_midb": {},
    }
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_attack_taps", "A3_damage_prevented",
            "A4_mill_combat", "A5_bolt_branch", "A6_cleanup")}
    notes = []

    p0 = PhaseClient("P0-mind")
    p1 = PhaseClient("P1-pass")
    p2 = PhaseClient("P2-pass")
    await p0.connect()
    await p1.connect()
    await p2.connect()
    say("clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=3)
    ST["game_code"] = p0.game_code or (sess or {}).get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    await p2.join(p0.game_code, deck(*P2_DECK))
    say("P1/P2 joined")

    # scenario provenance copy (written before the run so the uploaded copy
    # is byte-identical to what executed)
    with open(f"{EVDIR}/scenario_4836.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_4836.py").read())

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

    def unmark_rejected_iids(fresh):
        """A rejected interaction submission must be retryable: drop any
        answered iid named in a fresh rejection so the next tick answers
        the current opportunity again (cf. #4509 stall lesson)."""
        for rj in fresh:
            blob = json.dumps(rj, default=str)
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
                    say(f"unmarked rejected iid {iid}; will retry")

    def mulligan_pending_for(state, pid):
        for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
            ph = p.get("phase") or {}
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan(c, pid, tag, want_cards, st, state):
        if wf_of(state).get("type") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        rev = st.get("state_revision", -1)
        if acted(f"mull{pid}", rev):
            return True
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, MOUNTAIN, FOREST, PLAINS))
        mulls = ST["mulls"].get(pid, 0)
        keep = (lands >= 2 and all(w in hn for w in want_cards)) or mulls >= 2
        if keep:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep (lands={lands}, mulls={mulls})")
        else:
            ST["mulls"][pid] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{tag}] mulligan #{mulls + 1} (lands={lands})")
        return True

    async def bottom_after_mulligan(c, pid, tag, st, state, key_order):
        if wf_of(state).get("type") != "MulliganDecision":
            return False
        acts = merged_actions(st)
        sc = find_action(acts, "SelectCards")
        if not sc or ST["bottomed"].get(pid):
            return False
        pending = (wf_of(state).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))

        def bkey(oid):
            nm = lname(state, oid)
            for i, k in enumerate(key_order):
                if nm == k:
                    return i
            return len(key_order)
        picks = sorted(hand_ids(state, pid), key=bkey,
                       reverse=True)[:count]
        ST["bottomed"][pid] = True
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"[{tag}] bottoms {count}")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if wf_of(state).get("type") != "DiscardToHandSize":
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
                if "island" in tx or "mountain" in tx or "forest" in tx \
                        or "plains" in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            sub = {"interactionId": iid,
                   "response": {"type": resp.get("type") or "choose",
                                "data": {"choiceId": pick.get("id")}}}
            wire("discard_submit", {"who": tag, "iid": iid,
                                   "submission": sub})
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(sub)
            return True
        return False

    def resolve_target_candidate(opp, want):
        data = (opp.get("response") or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        avail = [ch for ch in cands
                 if (ch.get("status") or {}).get("type") == "available"]
        pool = avail or cands
        for ch in pool:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") if isinstance(s.get("data"), dict) else {}
                if want["kind"] == "player" and d.get("seat") == want["seat"]:
                    return ch.get("id"), ch
        for ch in pool:
            txt = json.dumps(ch).lower()
            if want["kind"] == "player" \
                    and f"player {want['seat']}" in txt:
                return ch.get("id"), ch
        return None, None

    async def answer_target_prompts(c, tag, st):
        vi = get_vi(st)
        if not vi:
            return False
        if ST["cast_awaiting"] and not ST["pending_target"]:
            ST["pending_target"] = dict(ST["cast_awaiting"]["want"])
        if not ST["pending_target"]:
            return False
        want = ST["pending_target"]
        answered = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            spec = (resp.get("data") or {}).get("spec") or {}
            if spec.get("type") != "sequence":
                continue
            cid, ch = resolve_target_candidate(opp, want)
            if cid is None:
                continue
            ST["target_prompts_seen"] += 1
            ST["answered_iids"].append(iid)
            # single-slot prompt (CR 601.2c): one choiceId per prompt
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
            say(f"[{tag}] targets {want['kind']} seat {want.get('seat')} "
                f"via candidate {cid}")
            wire("target_submission",
                 {"who": tag, "want": want, "choice_id": cid,
                  "submission": sub})
            await c.send_interaction(sub)
            answered = True
        return answered

    def clear_cast_awaiting(state):
        ca = ST["cast_awaiting"]
        if not ca:
            return
        if ca["name"] == BOLT:
            if BOLT in [lname(state, o) for o in
                        player_of(state, 0).get("graveyard", []) or []]:
                ST["cast_awaiting"] = None
                ST["pending_target"] = None
        if state.get("turn_number", 0) > ca.get("turn", 0):
            ST["cast_awaiting"] = None
            ST["pending_target"] = None

    def cast_spell_action(acts, state, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            oid = (a.get("data") or {}).get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def declare_attackers(c, pid, tag, attack_oids, defend_pid):
        st = c.latest
        acts = merged_actions(st)
        da = find_action(acts, "DeclareAttackers")
        if not da:
            return False
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = [[o, {"type": "Player", "data": defend_pid}]
                        for o in attack_oids]
        d["bands"] = []
        wire(f"{tag}_declare_attackers_submit", d)
        await c.send_action({"type": "DeclareAttackers", "data": d})
        say(f"[{tag}] declares attackers {attack_oids} vs P{defend_pid}")
        return True

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan(p0, 0, "P0", (MIND,), st, state):
            return
        if await bottom_after_mulligan(p0, 0, "P0", st, state,
                                       (MIND, BOLT, ISLAND, MOUNTAIN)):
            return
        if await handle_discard(p0, 0, "P0", st, state, (MIND, BOLT)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        fresh = drain_rejections(p0)
        for rj in fresh:
            obs["rejections"].append({"stage": ST["stage"], **rj})
            say(f"[P0] REJECTION in stage {ST['stage']}: "
                f"{json.dumps(rj, default=str)[:300]}")
        unmark_rejected_iids(fresh)
        if await answer_target_prompts(p0, "P0", st):
            return
        clear_cast_awaiting(state)
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa and not acted("p0ord", rev):
                await submit_as_is(p0, oa)
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad and not acted("p0dmg", rev):
                await submit_as_is(p0, ad)
                say("[P0] submits advertised AssignCombatDamage")
            return
        # mid-attack checkpoint: attackers declared, Mindskinner in the list
        if not ST["mid_attack_exported"]:
            atk = attacker_oids(state)
            mid_oid = next((o for o in atk if lname(state, o) == MIND), None)
            if mid_oid is not None:
                ST["mindskinner_oid"] = int(mid_oid)
                mc = await export_named("mid_attack")
                if mc is not None:
                    ST["mid_attack_exported"] = True
                    say(f"mid_attack: Mindskinner oid {mid_oid} attacking, "
                        f"tapped={get_obj(mc, mid_oid).get('tapped')}")
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            ready = [o for o in bf_ids(state, 0, MIND)
                     if can_attack_now(state, o)]
            if ready and not ST["pre_exported"]:
                say("P0 DeclareAttackers with attack-ready Mindskinner; "
                    "exporting PRE")
                pre = await export_named("pre")
                if pre is not None:
                    obs["life_pre"] = life_of(pre, 1)
                    obs["lib_pre"] = {i: lib_count(pre, i) for i in (1, 2)}
                    obs["gy_pre"] = {i: gy_count(pre, i) for i in (1, 2)}
                    ass["A1_setup_ok"] = "passed"
                    notes.append(
                        f"pre: P0 Mindskinner={len(ready)} ready, "
                        f"P1 life={obs['life_pre']}, "
                        f"P1 lib={obs['lib_pre'][1]}, "
                        f"P2 lib={obs['lib_pre'][2]}")
                    ST["pre_exported"] = True
            if ready:
                if ST["attack_turn"] is None:
                    ST["attack_turn"] = state.get("turn_number")
                    ST["mindskinner_oid"] = int(ready[0])
                await declare_attackers(p0, 0, "P0", [ready[0]], 1)
            else:
                await declare_attackers(p0, 0, "P0", [], 1)
            return
        if wtype == "DeclareBlockers" and state.get("active_player") != 0:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted("p0blk", rev):
                d = copy.deepcopy(db.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers",
                                      "data": d})
            return
        if not my_priority(state, 0):
            return
        phase = state.get("phase") or ""
        turn = state.get("turn_number")
        # mid-combat checkpoint at first PostCombatMain priority of the
        # attack turn (export BEFORE acting so the checkpoint is clean)
        if (ST["pre_exported"] and not ST["mid_combat_exported"]
                and phase == "PostCombatMain"
                and ST["attack_turn"] is not None
                and turn == ST["attack_turn"]):
            mc = await export_named("mid_combat")
            if mc is not None:
                obs["life_midc"] = life_of(mc, 1)
                obs["lib_midc"] = {i: lib_count(mc, i) for i in (1, 2)}
                obs["gy_midc"] = {i: gy_count(mc, i) for i in (1, 2)}
                ST["mid_combat_exported"] = True
                say(f"mid_combat: P1 life={obs['life_midc']}, "
                    f"P1 lib={obs['lib_midc'][1]} gy={obs['gy_midc'][1]}, "
                    f"P2 lib={obs['lib_midc'][2]} gy={obs['gy_midc'][2]}")
            return
        # bolt-resolution checkpoint: Bolt in P0 graveyard, stack quiet.
        # Checked BEFORE the cast branch so we never cast a second Bolt.
        if (ST.get("bolt_cast") and not ST["mid_bolt_exported"]
                and not (state.get("stack") or [])):
            p0_gy = [lname(state, o) for o in
                     player_of(state, 0).get("graveyard", []) or []]
            if BOLT in p0_gy:
                mb = await export_named("mid_bolt")
                if mb is not None:
                    obs["life_midb"] = life_of(mb, 1)
                    obs["lib_midb"] = {i: lib_count(mb, i) for i in (1, 2)}
                    obs["gy_midb"] = {i: gy_count(mb, i) for i in (1, 2)}
                    ST["mid_bolt_exported"] = True
                    ST["bolt_done"] = True
                    say(f"mid_bolt: P1 life={obs['life_midb']}, "
                        f"P1 lib={obs['lib_midb'][1]} gy={obs['gy_midb'][1]}, "
                        f"P2 lib={obs['lib_midb'][2]} gy={obs['gy_midb'][2]}")
                return
        own_main = state.get("active_player") == 0 \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and not (state.get("stack") or [])
        if own_main:
            # bolt branch: mid-combat captured, exactly one Bolt, red mana
            if (ST["mid_combat_exported"] and not ST["bolt_done"]
                    and not ST.get("bolt_cast")
                    and BOLT in hand_lnames(state, 0)
                    and untapped_lands(state, 0, MOUNTAIN)):
                ca = cast_spell_action(acts, state, BOLT)
                if ca and not acted("bolt", rev):
                    ST["bolt_cast"] = True
                    ST["cast_awaiting"] = {
                        "name": BOLT,
                        "turn": turn,
                        "want": {"kind": "player", "seat": 1},
                    }
                    ST["pending_target"] = {"kind": "player", "seat": 1}
                    say("[P0] casts Lightning Bolt at P1 (non-combat "
                        "damage branch)")
                    wire("cast_p0_bolt", ca)
                    await submit_as_is(p0, ca)
                    return
            # cast the Mindskinner
            if (not ST["mind_cast"]
                    and MIND in hand_lnames(state, 0)
                    and len(untapped_lands(state, 0, ISLAND)) >= 3
                    and not bf_ids(state, 0, MIND)):
                ca = cast_spell_action(acts, state, MIND)
                if ca and not acted("mind", rev):
                    say("[P0] casts The Mindskinner")
                    wire("cast_p0_mind", ca)
                    await submit_as_is(p0, ca)
                    ST["mind_cast"] = True
                    return
            # land drop (retry every tick; no kept-flag -- cf. #6690)
            for a in acts:
                if a["type"] == "PlayLand":
                    oid = (a.get("data") or {}).get("object_id")
                    if oid is not None and not acted("p0land", rev):
                        await submit_as_is(p0, a)
                        return
        # default: pass priority when it is ours to pass. Gated on
        # my_priority so we never fire out-of-turn passes (an applied
        # out-of-turn pass is a no-op that still burns the revision
        # guard). The action is resolved BEFORE marking acted, so a tick
        # with no PassPriority offered does not suppress retries.
        if my_priority(state, 0):
            pa = find_action(acts, "PassPriority")
            if pa and not acted("p0pass", rev):
                await submit_as_is(p0, pa)

    async def passive_tick(c, pid, tag, land_name):
        st = c.latest
        acts = merged_actions(st)
        state = st["state"]
        rev = st.get("state_revision", -1)
        if await mulligan(c, pid, tag, (), st, state):
            return
        if await bottom_after_mulligan(c, pid, tag, st, state,
                                       (land_name,)):
            return
        if await handle_discard(c, pid, tag, st, state, ()):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(c, a)
                return
        wtype = wf_of(state).get("type") or ""
        fresh = drain_rejections(c)
        for rj in fresh:
            obs["rejections"].append({"stage": ST["stage"], **rj})
            say(f"[{tag}] REJECTION in stage {ST['stage']}: "
                f"{json.dumps(rj, default=str)[:300]}")
        unmark_rejected_iids(fresh)
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa and not acted(f"{tag}ord", rev):
                await submit_as_is(c, oa)
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad and not acted(f"{tag}dmg", rev):
                await submit_as_is(c, ad)
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == pid:
            await declare_attackers(c, pid, tag, [], 0)
            return
        if wtype == "DeclareBlockers":
            db = find_action(acts, "DeclareBlockers")
            if db and not acted(f"{tag}blk", rev):
                d = copy.deepcopy(db.get("data", {}))
                d["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": d})
                say(f"[{tag}] declares no blockers (Mindskinner can't be "
                    f"blocked anyway)")
            return
        if not my_priority(state, pid):
            return
        phase = state.get("phase") or ""
        own_main = state.get("active_player") == pid \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and not (state.get("stack") or [])
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    oid = (a.get("data") or {}).get("object_id")
                    if oid is not None and lname(state, int(oid)) == land_name \
                            and not acted(f"{tag}land", rev):
                        await submit_as_is(c, a)
                        return
        if my_priority(state, pid):
            pa = find_action(acts, "PassPriority")
            if pa and not acted(f"{tag}pass", rev):
                await submit_as_is(c, pa)

    async def p1_tick(st, acts, state):
        await passive_tick(p1, 1, "P1", FOREST)

    async def p2_tick(st, acts, state):
        await passive_tick(p2, 2, "P2", PLAINS)

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
            ST["turns_seen"].add(state.get("turn_number"))
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")
                say(f"tick error {tag}: {e!r}")

    def evaluate():
        def load_env(name):
            p = f"{EVDIR}/{name}.json"
            if not os.path.exists(p):
                return None
            try:
                return json.loads(open(p).read())["state"]
            except Exception as e:
                notes.append(f"{name}.json reload failed: {e}")
                return None

        ma_st = load_env("mid_attack")

        # A2: the declared Mindskinner tapped (the reported observation)
        if ma_st is not None and ST["mindskinner_oid"]:
            oid = ST["mindskinner_oid"]
            listed = oid in attacker_oids(ma_st)
            tapped = bool(get_obj(ma_st, oid).get("tapped"))
            power = get_obj(ma_st, oid).get("power")
            if listed and tapped:
                ass["A2_attack_taps"] = "passed"
                notes.append(f"A2 passed: Mindskinner oid {oid} in "
                             f"combat.attackers, tapped={tapped}, "
                             f"power={power}")
            else:
                ass["A2_attack_taps"] = "failed"
                notes.append(f"A2 failed: oid {oid} listed={listed} "
                             f"tapped={tapped} power={power}")
        else:
            ass["A2_attack_taps"] = "failed"
            notes.append("A2 failed: no mid_attack checkpoint with the "
                         "Mindskinner as an attacker")

        # A3: damage prevented (P1 life unchanged across combat)
        if obs["life_pre"] is not None and obs["life_midc"] is not None:
            if obs["life_midc"] == obs["life_pre"]:
                ass["A3_damage_prevented"] = "passed"
                notes.append(f"A3 passed: P1 life {obs['life_pre']} -> "
                             f"{obs['life_midc']} (10 combat damage prevented)")
            else:
                ass["A3_damage_prevented"] = "failed"
                notes.append(f"A3 FAILED: P1 life {obs['life_pre']} -> "
                             f"{obs['life_midc']}; combat damage was NOT "
                             f"prevented")
        else:
            notes.append("A3 not evaluated (missing pre/mid_combat life)")

        # A4: each opponent mills exactly 10 from the combat replacement
        if obs["lib_pre"] and obs["lib_midc"]:
            parts = []
            ok = True
            for i in (1, 2):
                milled = obs["lib_pre"][i] - obs["lib_midc"][i]
                gyed = obs["gy_midc"][i] - obs["gy_pre"][i]
                parts.append(f"P{i} lib-{milled}/gy+{gyed}")
                if milled != 10 or gyed != 10:
                    ok = False
            ass["A4_mill_combat"] = "passed" if ok else "failed"
            notes.append(f"A4 {'passed' if ok else 'FAILED'}: "
                         + ", ".join(parts) + " (expected lib-10/gy+10 each)")
        else:
            notes.append("A4 not evaluated (missing pre/mid_combat counts)")

        # A5: Bolt branch -- life unchanged, each opponent mills exactly 3
        if obs["lib_midc"] and obs["lib_midb"]:
            parts = []
            ok = True
            life_ok = obs["life_midb"] == obs["life_midc"]
            for i in (1, 2):
                milled = obs["lib_midc"][i] - obs["lib_midb"][i]
                gyed = obs["gy_midb"][i] - obs["gy_midc"][i]
                parts.append(f"P{i} lib-{milled}/gy+{gyed}")
                if milled != 3 or gyed != 3:
                    ok = False
            ass["A5_bolt_branch"] = "passed" if (ok and life_ok) else "failed"
            notes.append(f"A5 {'passed' if (ok and life_ok) else 'FAILED'}: "
                         + ", ".join(parts)
                         + f"; P1 life {obs['life_midc']} -> {obs['life_midb']} "
                         + "(expected lib-3/gy+3 each, life unchanged)")
        else:
            notes.append("A5 not evaluated (bolt branch did not complete; "
                         f"bolt_cast={ST.get('bolt_cast')}, "
                         f"mid_bolt_exported={ST['mid_bolt_exported']})")

        # A6: cleanup
        post = load_env("post")
        if post is not None:
            slen = len(post.get("stack", []) or [])
            wft = (post.get("waiting_for") or {}).get("type")
            if slen == 0 and wft == "Priority":
                ass["A6_cleanup"] = "passed"
                notes.append(f"A6 passed: post.json stack empty, wf={wft}, "
                             f"turn={post.get('turn_number')}, "
                             f"phase={post.get('phase')}")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"A6 failed: post stack={slen}, wf={wft}")
        else:
            notes.append("A6 not evaluated (no post.json)")

        # Verdict
        failed_core = [k for k in ("A3_damage_prevented", "A4_mill_combat",
                                   "A5_bolt_branch")
                       if ass[k] == "failed"]
        passed_core = all(ass[k] == "passed" for k in
                          ("A2_attack_taps", "A3_damage_prevented",
                           "A4_mill_combat", "A5_bolt_branch"))
        if failed_core:
            verdict = "reproduced"
            notes.append("verdict reproduced on v0.84.0: failing assertions: "
                         + ", ".join(failed_core))
        elif passed_core:
            verdict = "not-reproduced"
            notes.append("verdict not-reproduced on v0.84.0: the full reported "
                         "path completes (combat damage prevented, each "
                         "opponent mills 10; Bolt damage prevented, each "
                         "opponent mills 3). Not tested on the report's "
                         "original build; not a fix claim.")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: scenario did not reach the "
                         "evaluation checkpoints: " + json.dumps(ass))
        return verdict

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        for tag in ("post",):
            if tag not in ST["exports"]:
                await export_named(tag)
        verdict = evaluate()
        # server log excerpts (this run owns the server in runs/<RUN_ID>/)
        try:
            logp = f"{BACKFILL}/runs/{RUN_ID}/server.log"
            lines = open(logp, errors="replace").read().splitlines()
            interesting = [l for l in lines
                           if "ERROR" in l or "WARN" in l or "panic" in l]
            tail = lines[-40:]
            with open(f"{EVDIR}/server_excerpts.log", "w") as f:
                f.write(f"# server.log excerpts for run {RUN_ID} "
                        f"(v0.84.0, 127.0.0.1:9374)\n")
                f.write(f"# error/warn/panic lines: {len(interesting)}\n")
                for l in interesting[-60:]:
                    f.write(l + "\n")
                f.write("# --- last 40 lines ---\n")
                for l in tail:
                    f.write(l + "\n")
            say(f"server excerpts: {len(interesting)} error/warn lines, "
                f"{len(lines)} total")
        except Exception as e:
            notes.append(f"server excerpts failed: {e}")
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 71,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4836.py", "rb").read()
            ).hexdigest(),
            "format_config": "default Bo1 (3 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "issue_title": "The Mindskinner taps on attack but does not mill",
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k != "_fresh_rej"},
            "driver_state": {
                "mind_cast": ST["mind_cast"],
                "bolt_cast": ST["bolt_cast"],
                "bolt_done": ST["bolt_done"],
                "attack_turn": ST["attack_turn"],
                "mindskinner_oid": ST["mindskinner_oid"],
                "target_prompts_seen": ST["target_prompts_seen"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(t for t in ST["turns_seen"]
                                     if t is not None),
                "exports": ST["exports"],
                "rejections": len(obs["rejections"]),
            },
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "8x Mindskinner / 12x Lightning Bolt deck density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "Not tested on the report's original 2026-07-01 build; "
                "verdict is scoped to v0.84.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "The Undead Alchemist interaction from the 2026-08-02 "
                "reopen note was not exercised: the mill events themselves "
                "are asserted, but their consumption by Alchemist's exile "
                "replacement is not tested.",
            ],
            "setup_line": "P0: 8x The Mindskinner + 12x Lightning Bolt + "
                          "22x Island + 18x Mountain (mulligan to Mindskinner "
                          "+ lands, cast Mindskinner {U}{U}{U}, attack P1); "
                          "P1/P2: 60x land each, passive (never attack)",
            "contract_line": "The Mindskinner attacks as a 10/1: it taps, "
                             "its 10 combat damage to P1 is prevented, and "
                             "EACH opponent (P1, P2) mills 10; then "
                             "Lightning Bolt at P1 is prevented and each "
                             "opponent mills 3",
            "history": [
                {"run_id": "20260909-4836", "release": "v0.78.0",
                 "protocol": 68, "verdict": "not-reproduced",
                 "evidence_commit":
                 "33d3b9c72ab260278a88925f5e07098dbb0a429d",
                 "note": "original validation; ledger entry lost in a VM "
                         "replacement; v0.78.0 result stale after pin "
                         "advanced to v0.84.0"},
            ],
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    p2t = asyncio.create_task(tick(p2, 2, "P2", p2_tick))
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            s = (p0.latest or {}).get("state") or {}
            turn = s.get("turn_number", 0) or 0
            phase = s.get("phase")
            wtype = (s.get("waiting_for") or {}).get("type")
            # post export: bolt branch done and the game advanced past the
            # attack turn
            if (ST["mid_bolt_exported"] and not ST["post_exported"]
                    and ST["attack_turn"] is not None
                    and turn > ST["attack_turn"]):
                await export_named("post")
                ST["post_exported"] = True
                ST["stage"] = "done"
                say("post exported; finishing")
                break
            if turn >= 40:
                notes.append("turn 40 reached without completing; bailing out")
                say("turn 40 bail-out; finishing")
                break
            # watchdog: if any client's revision goes stale >45s while the
            # run is live, log that client's view for diagnosis
            for _tag, _c in (("P0", p0), ("P1", p1), ("P2", p2)):
                _rev = (_c.latest or {}).get("state_revision")
                _wkey = f"wd_{_tag}"
                _last = ST.get(_wkey)
                if _rev != (_last or {}).get("rev"):
                    ST[_wkey] = {"rev": _rev, "t": time.time()}
                elif ST["stage"] != "done" and s \
                        and time.time() - ST[_wkey]["t"] > 45:
                    _s = (_c.latest or {}).get("state") or {}
                    say(f"[watchdog] {_tag} revision {_rev} stale "
                        f"{time.time() - ST[_wkey]['t']:.0f}s: "
                        f"turn={_s.get('turn_number')} "
                        f"phase={_s.get('phase')} "
                        f"wf={(_s.get('waiting_for') or {}).get('type')} "
                        f"pp={_s.get('priority_player')}")
                    ST[_wkey]["t"] = time.time()  # re-arm
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={turn} phase={phase} wf={wtype} "
                    f"mind_bf={bf_ids(s, 0, MIND)} "
                    f"life1={life_of(s, 1)} P1lib={lib_count(s, 1)} "
                    f"P2lib={lib_count(s, 2)} mind_cast={ST['mind_cast']} "
                    f"atk_turn={ST['attack_turn']} "
                    f"bolt_cast={ST['bolt_cast']} bolt_done={ST['bolt_done']}")
    finally:
        p0t.cancel()
        p1t.cancel()
        p2t.cancel()
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
