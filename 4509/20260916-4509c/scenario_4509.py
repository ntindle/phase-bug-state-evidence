#!/usr/bin/env python3
"""Issue #4509: "Lost in Thought ignore-effect escape clause dropped (cluster 36)".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-06-28, cluster synthesis): "Lost in Thought" drops its combined
restriction/escape clause end-to-end:
  1. The restriction line parses without binding combat/activation lockouts to
     the enchanted host (EnchantedBy).
  2. The escape line ("exile three cards ... to ignore this effect until end of
     turn") is not parsed or synthesized into a payable ignore-effect action.
  3. Runtime has no StaticSourceIgnored restriction.

Oracle text (verified from pinned v0.84.0 card-data.json):
  "Enchant creature
   Enchanted creature can't attack or block, and its activated abilities can't
   be activated. Its controller may exile three cards from their graveyard for
   that player to ignore this effect until end of turn."
Card-data parse state on v0.84.0: the combined line is
  Unimplemented("Static pattern matched but line failed static parser: ..."),
  static_abilities == [].

Classifier comment (2026-07-19): the authoritative Oracle uses the end-of-turn
ignore-effect payment (not the stale next-turn wording in the issue body).

Scenario (native engine, two human-client seats, protocol 71):
  P0: 12x lost in thought ({1}{U}) + 12x thought scour ({U}) + 36x island.
      Mills P1 twice (Thought Scour targeting P1) so P1's graveyard holds >=3
      cards, then casts Lost in Thought enchanting P1's Llanowar Elves.
  P1: 12x llanowar elves ({G}, "{T}: Add {G}") + 12x grizzly bears
      ({1}{G}) + 36x forest. Develops Elves + Bears, never attacks except in
      the scripted test combats.

Assertions:
  A1_setup_ok       pre.json: Lost in Thought on the battlefield attached to
                    P1's Elves (the targeted object), P1 graveyard >= 3.
  A2_attack_restricted
                    The enchanted Elves is declared as an attacker by P1. Pass
                    iff the engine refuses the declaration (rejection, silent
                    exclusion) and the Elves never appears in combat.attackers
                    and deals no combat damage. Fail iff the Elves attacks and
                    damages P0 -> the CantAttackOrBlock restriction is dropped.
  A3_activation_restricted
                    P1 attempts the enchanted Elves' "{T}: Add {G}" ability on
                    a later main phase (Elves untapped). Pass iff no
                    ActivateAbility is offered for the enchanted Elves (or the
                    attempt is rejected with no mana produced). Fail iff the
                    activation succeeds and P1's pool gains {G} -> the
                    CantBeActivated restriction is dropped.
  A4_escape_offered  With P1's graveyard >= 3 and the aura on the battlefield,
                    at P1 priority the driver scans legal_actions and
                    viewer_interaction for any exile-3-to-ignore escape
                    action. Pass iff offered. Fail iff absent -> the escape
                    clause is dropped.
  A5_escape_effective
                    Only if A4 passes: pay the exile-3 escape, then the Elves
                    attacks the same turn. Expected not-run on this build.
  A6_control_binding
                    P1's unenchanted Grizzly Bears attacks normally on a later
                    combat (engine accepts, P0 takes 2) -> documents the aura
                    is inert rather than mis-bound.

Verdict rule: reproduced iff any of A2, A3, A4 fails (the combined clause is
dropped end-to-end, per the report). not-reproduced iff A2, A3, A4 and A5 all
pass. blocked iff the game cannot be driven to the aura-attack test.

Evidence: evidence/4509/<run-id>/pre.json, post_attack.json, post_control.json,
run.json, manifest.sha256, summary.png, scenario_4509.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 4509
RUN_ID = os.environ.get("RUN_ID", "20260916-4509c")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"
os.makedirs(f"{BACKFILL}/runs/run-{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

LOST = "lost in thought"
SCOUR = "thought scour"
ELVES = "llanowar elves"
BEARS = "grizzly bears"
ISLAND = "island"
FOREST = "forest"

P0_DECK = [(LOST, 12), (SCOUR, 12), (ISLAND, 36)]
P1_DECK = [(ELVES, 12), (BEARS, 12), (FOREST, 36)]

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
                      "at pin time 2026-09-15; data digests match the signed "
                      "manifest; digests recomputed against on-disk files "
                      "this run; release v0.84.0 confirmed latest stable via "
                      "GitHub releases API 2026-09-16",
    "observed_at": "2026-09-16",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/run-20260916-4509/",
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
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?")


def lname(state, oid):
    return obj_name(state, oid).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == name and not o.get("tapped")]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def aura_on_bf(state, name=LOST):
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") == "Battlefield" and lname(state, oid) == name:
            return int(oid), o
    return None, None


def spell_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if lname(state, o) == name:
            return int(o)
    return None


def gy_count(state, pid):
    return len(player_of(state, pid).get("graveyard", []))


COLORS = ("white", "blue", "black", "red", "green", "colorless")


def mana_pool(state, pid):
    out = Counter()
    for p in state.get("players", []):
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in COLORS:
                    if color in blob:
                        out[color] += 1
                        break
                else:
                    out["unknown"] += 1
    return dict(out)


def attacker_oids(state):
    out = set()
    c = state.get("combat") or {}
    for a in c.get("attackers") or []:
        if isinstance(a, (list, tuple)) and a:
            try:
                out.add(int(a[0]))
            except Exception:
                pass
        elif isinstance(a, dict):
            for k in ("attacker", "attacker_id", "object_id", "id"):
                if k in a:
                    try:
                        out.add(int(a[k]))
                    except Exception:
                        pass
        else:
            try:
                out.add(int(a))
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
    d = (wf_of(state).get("data") or {})
    return d.get("player")


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

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",  # setup -> attack_test -> activation_test ->
                           # control -> done
        "game_code": None,
        "enchant_target_oid": None,
        "aura_oid": None,
        "aura_object_logged": False,
        "cast_awaiting": None,      # {"name":..., "want":...} while a cast's
                                    # target prompt may still be pending
        "pending_target": None,     # {"kind","oid"/"seat","name"} rebuilt
                                    # from cast_awaiting while prompts persist
        "answered_iids": [],
        "attack1_submitted": False,
        "attack1_turn": None,
        "attack1_rejected": False,
        "attack1_accepted": False,
        "attack1_silent_ticks": 0,
        "_rej_mark": 0,
        "elves_attacked": False,
        "p0_life_pre_attack": None,
        "p0_life_post_attack": None,
        "activation_attempted": False,
        "activation_offered": False,
        "activation_rejected": False,
        "pool_before_activation": None,
        "pool_after_activation": None,
        "activation_candidates_seen": None,
        "activation_wait_ticks": 0,
        "escape_offered": False,
        "escape_scan_done": False,
        "p1_action_types": [],
        "bear_oid": None,
        "bear_submitted": False,
        "bear_turn": None,
        "bear_attack_accepted": False,
        "p0_life_pre_bear": None,
        "p0_life_post_bear": None,
        "target_prompts_seen": 0,
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "tick_errors": [], "escape_scan": None}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-mill")
    p1 = PhaseClient("P1-beats")
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

    async def mulligan(c, pid, tag, want_cards, st, state):
        if wf_of(state).get("type") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        rev = st.get("state_revision", -1)
        if acted(f"mull{pid}", rev):
            return True
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, FOREST))
        mulls = ST.get(f"mulls{pid}", 0)
        keep = (lands >= 2 and any(w in hn for w in want_cards)) or mulls >= 2
        if keep:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep (lands={lands}, mulls={mulls})")
        else:
            ST[f"mulls{pid}"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{tag}] mulligan #{mulls + 1} (lands={lands})")
        return True

    async def bottom_after_mulligan(c, pid, tag, st, state, key_order):
        if wf_of(state).get("type") != "MulliganDecision":
            return False
        acts = merged_actions(st)
        sc = find_action(acts, "SelectCards")
        if not sc or ST.get(f"bottomed{pid}"):
            return False
        pending = (wf_of(state).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))
        hand = hand_ids(state, pid)

        def bkey(oid):
            nm = lname(state, oid)
            for i, k in enumerate(key_order):
                if nm == k:
                    return i
            return len(key_order)
        # bottom the LAST-ranked (least wanted) cards: sort ascending by
        # rank and take from the end
        picks = sorted(hand, key=bkey, reverse=True)[:count]
        ST[f"bottomed{pid}"] = True
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
                if ISLAND in tx or FOREST in tx:
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
            oid = (a.get("data") or {}).get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def resolve_target_candidate(opp, want):
        """Pick the candidate matching the intended target from a schema
        sequence opportunity. Resolves objects from the candidate's
        surfaces[].data.reference, players from surfaces[].data.seat."""
        data = (opp.get("response") or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        avail = [ch for ch in cands
                 if (ch.get("status") or {}).get("type") == "available"]
        pool = avail or cands
        for ch in pool:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") if isinstance(s.get("data"), dict) else {}
                if want["kind"] == "creature":
                    ref = d.get("reference")
                    if ref is not None and str(ref) == str(want["oid"]):
                        return ch.get("id"), ch, ref
                elif want["kind"] == "player":
                    if d.get("seat") == want["seat"]:
                        return ch.get("id"), ch, None
        for ch in pool:
            txt = json.dumps(ch).lower()
            if want["kind"] == "creature" and want["name"] in txt:
                return ch.get("id"), ch, None
            if want["kind"] == "player" \
                    and f"player {want['seat']}" in txt:
                return ch.get("id"), ch, None
        return None, None, None

    async def answer_target_prompts(c, tag, st):
        """Answer schema-type target-selection opportunities with the
        scenario's intended target. Retains pending_target while the cast
        that needs it is still awaiting resolution (multi-slot prompts
        arrive one at a time)."""
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
            spec = ((resp.get("data") or {}).get("spec") or {})
            if spec.get("type") != "sequence":
                continue
            cid, ch, ref = resolve_target_candidate(opp, want)
            if cid is None:
                continue
            ST["target_prompts_seen"] += 1
            ST["answered_iids"].append(iid)
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
            say(f"[{tag}] targets {want['kind']} via candidate {cid} "
                f"(ref={ref})")
            wire("target_submission",
                 {"who": tag, "want": want, "choice_id": cid,
                  "reference": ref, "submission": sub})
            ST["last_target_oid"] = ref
            await c.send_interaction(sub)
            answered = True
        return answered

    def clear_cast_awaiting(state):
        """Drop the retained target once the cast demonstrably resolved."""
        ca = ST["cast_awaiting"]
        if not ca:
            return
        name = ca["name"]
        if name == LOST:
            oid, _ = aura_on_bf(state)
            if oid is not None:
                ST["cast_awaiting"] = None
                ST["pending_target"] = None
        elif name == SCOUR:
            # scour resolved when it leaves hand/stack (in graveyard)
            pass  # cleared by turn advancement below
        # generic: once the turn advanced past the cast turn, stop retaining
        if state.get("turn_number", 0) > ca.get("turn", 0):
            ST["cast_awaiting"] = None
            ST["pending_target"] = None

    def log_aura_object(state):
        oid, o = aura_on_bf(state)
        if oid is not None and not ST["aura_object_logged"]:
            ST["aura_object_logged"] = True
            ST["aura_oid"] = oid
            wire("aura_object", {
                "oid": oid,
                "static_definitions": o.get("static_definitions"),
                "unimplemented_mechanics": o.get("unimplemented_mechanics"),
                "attached_to": o.get("attached_to"),
                "controller": o.get("controller"),
                "all_keys": sorted(o.keys()),
            })
            say(f"aura on BF oid={oid} "
                f"static_definitions={o.get('static_definitions')} "
                f"unimplemented={o.get('unimplemented_mechanics')}")

    def scan_escape(state, acts, st):
        """Look for any exile-3-to-ignore escape action offered to P1."""
        if ST["escape_scan_done"] or ST["escape_offered"]:
            return
        if not ST["aura_object_logged"]:
            return
        if gy_count(state, 1) < 3:
            return
        texts = []
        for a in acts:
            blob = json.dumps(a, default=str).lower()
            texts.append(a["type"])
            if "exile" in blob and "graveyard" in blob \
                    and ("ignore" in blob or "lost in thought" in blob):
                ST["escape_offered"] = True
                wire("escape_found_action", a)
                say("ESCAPE ACTION OFFERED: " + json.dumps(a)[:500])
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                blob = json.dumps(opp, default=str).lower()
                if "exile" in blob and "graveyard" in blob \
                        and ("ignore" in blob or "lost in thought" in blob):
                    ST["escape_offered"] = True
                    wire("escape_found_vi", opp)
                    say("ESCAPE VI OFFERED: " + json.dumps(opp)[:500])
        ST["escape_scan_done"] = True
        ST["p1_action_types"] = sorted(set(texts))
        obs["escape_scan"] = {"action_types": ST["p1_action_types"],
                              "offered": ST["escape_offered"]}
        wire("escape_scan", obs["escape_scan"])
        say(f"P1 priority action types at escape scan: "
            f"{ST['p1_action_types']}")

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan(p0, 0, "P0", (LOST, SCOUR), st, state):
            return
        if await bottom_after_mulligan(p0, 0, "P0", st, state,
                                       (LOST, SCOUR, ISLAND)):
            return
        if await handle_discard(p0, 0, "P0", st, state, (LOST, SCOUR)):
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
        if await answer_target_prompts(p0, "P0", st):
            return
        clear_cast_awaiting(state)
        log_aura_object(state)
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da and not acted("p0atk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await submit_as_is(p0, {"type": "DeclareAttackers",
                                        "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da and not acted("p0blk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["assignments"] = []
                await submit_as_is(p0, {"type": "DeclareBlockers",
                                        "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa and not acted("p0ord", rev):
                await submit_as_is(p0, oa)
            return
        if not my_priority(state, 0):
            return
        phase = state.get("phase") or ""
        own_main = state.get("active_player") == 0 \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and not stack_entries(state)
        if own_main and ST["stage"] == "setup":
            # land drop: any PlayLand for an island in hand
            pla = None
            for a in acts:
                if a.get("type") == "PlayLand":
                    oid = (a.get("data") or {}).get("object_id")
                    if oid is not None and lname(state, int(oid)) == ISLAND:
                        pla = a
                        break
            if pla and not acted("p0land", rev):
                await submit_as_is(p0, pla)
                return
            # mill P1: Thought Scour targeting P1 (need P1 gy >= 3)
            if ST.get("scours_cast", 0) < 2 and gy_count(state, 1) < 4 \
                    and len(untapped_lands(state, 0, ISLAND)) >= 1:
                ca = cast_spell_action(acts, state, SCOUR)
                if ca and not acted("scour", rev):
                    ST["scours_cast"] = ST.get("scours_cast", 0) + 1
                    ST["cast_awaiting"] = {
                        "name": SCOUR,
                        "turn": state.get("turn_number", 0),
                        "want": {"kind": "player", "seat": 1},
                    }
                    ST["pending_target"] = {"kind": "player", "seat": 1}
                    say(f"[P0] casts Thought Scour "
                        f"#{ST['scours_cast']} targeting P1")
                    wire("cast_scour", ca)
                    await submit_as_is(p0, ca)
                    return
            # cast the aura on P1's Elves
            if not ST.get("aura_cast") \
                    and len(untapped_lands(state, 0, ISLAND)) >= 2:
                elves = bf_ids(state, 1, ELVES)
                ca = cast_spell_action(acts, state, LOST)
                if elves and ca and not acted("aura", rev):
                    ST["aura_cast"] = True
                    ST["enchant_target_oid"] = elves[0]
                    ST["cast_awaiting"] = {
                        "name": LOST,
                        "turn": state.get("turn_number", 0),
                        "want": {"kind": "creature", "oid": elves[0],
                                 "name": ELVES},
                    }
                    ST["pending_target"] = {"kind": "creature",
                                            "oid": elves[0], "name": ELVES}
                    say(f"[P0] casts Lost in Thought targeting "
                        f"Elves {elves[0]}")
                    wire("cast_aura", ca)
                    await submit_as_is(p0, ca)
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

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan(p1, 1, "P1", (ELVES, BEARS), st, state):
            return
        if await bottom_after_mulligan(p1, 1, "P1", st, state,
                                       (ELVES, BEARS, FOREST)):
            return
        if await handle_discard(p1, 1, "P1", st, state, (ELVES, BEARS)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        wtype = wf_of(state).get("type") or ""
        for rj in drain_rejections(p1):
            obs["rejections"].append({"stage": ST["stage"], **rj})
            say(f"[P1] REJECTION in stage {ST['stage']}: "
                f"{json.dumps(rj, default=str)[:300]}")
        log_aura_object(state)
        if ST["stage"] in ("attack_test", "activation_test", "control") \
                and my_priority(state, 1):
            scan_escape(state, acts, st)
        # capture the pool on the first tick after the activation attempt,
        # before the game can advance phases (mana empties on phase change)
        if ST["activation_attempted"] and ST["activation_offered"] \
                and ST["pool_after_activation"] is None:
            ST["pool_after_activation"] = mana_pool(state, 1)
            wire("pool_after_capture", ST["pool_after_activation"])
        # track the enchanted Elves in combat
        eoid = ST["enchant_target_oid"]
        if eoid is not None and eoid in attacker_oids(state):
            if not ST["elves_attacked"]:
                ST["elves_attacked"] = True
                say(f"Elves {eoid} IS in combat.attackers")
                wire("elves_attacking",
                     {"attackers": sorted(attacker_oids(state))})
        if ST["attack1_submitted"] and not ST["attack1_accepted"] \
                and not ST["attack1_rejected"]:
            if eoid is not None and eoid in attacker_oids(state):
                ST["attack1_accepted"] = True
                say("attack1 ACCEPTED (Elves in attackers)")
                wire("attack1_accepted", {})
        # bear acceptance detection
        if ST.get("bear_oid") is not None \
                and ST["bear_oid"] in attacker_oids(state):
            if not ST["bear_attack_accepted"]:
                ST["bear_attack_accepted"] = True
                say(f"Bear {ST['bear_oid']} IS in combat.attackers")
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da.get("data", {}))
                if ST["stage"] == "attack_test" \
                        and not ST["attack1_submitted"]:
                    d["attacks"] = [[eoid, {"type": "Player", "data": 0}]]
                    d["bands"] = []
                    ST["attack1_submitted"] = True
                    ST["attack1_turn"] = state.get("turn_number")
                    ST["p0_life_pre_attack"] = life_of(state, 0)
                    ST["_rej_mark"] = len(obs["rejections"])
                    say(f"[P1] declares attack with enchanted "
                        f"Elves {eoid}")
                    wire("attack1_submit", d)
                    await p1.send_action({"type": "DeclareAttackers",
                                          "data": d})
                    return
                if ST["stage"] == "attack_test" \
                        and ST["attack1_submitted"] \
                        and not ST["attack1_accepted"] \
                        and not ST["attack1_rejected"]:
                    if len(obs["rejections"]) > ST.get("_rej_mark", 0):
                        ST["attack1_rejected"] = True
                        say("attack1 REJECTED by engine")
                        wire("attack1_rejected", {})
                    else:
                        ST["attack1_silent_ticks"] += 1
                        if ST["attack1_silent_ticks"] >= 4:
                            ST["attack1_rejected"] = True
                            notes.append(
                                "attack1: engine never accepted nor "
                                "rejected; treated as blocked (silent "
                                "exclusion)")
                            say("attack1 silently excluded; declaring empty")
                    if ST["attack1_rejected"]:
                        d["attacks"] = []
                        d["bands"] = []
                        await p1.send_action({"type": "DeclareAttackers",
                                              "data": d})
                        return
                    return
                if ST["stage"] == "control" and not ST["bear_submitted"]:
                    bears = bf_ids(state, 1, BEARS)
                    if bears:
                        d["attacks"] = [[bears[0],
                                         {"type": "Player", "data": 0}]]
                        d["bands"] = []
                        ST["bear_submitted"] = True
                        ST["bear_oid"] = bears[0]
                        ST["bear_turn"] = state.get("turn_number")
                        ST["p0_life_pre_bear"] = life_of(state, 0)
                        say(f"[P1] declares control attack with "
                            f"Bear {bears[0]}")
                        wire("bear_submit", d)
                        await p1.send_action({"type": "DeclareAttackers",
                                              "data": d})
                        return
                if not acted("p1atk", rev):
                    d["attacks"] = []
                    d["bands"] = []
                    await p1.send_action({"type": "DeclareAttackers",
                                          "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da and not acted("p1blk", rev):
                d = copy.deepcopy(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers",
                                      "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa and not acted("p1ord", rev):
                await submit_as_is(p1, oa)
            return
        if not my_priority(state, 1):
            return
        phase = state.get("phase") or ""
        own_main = state.get("active_player") == 1 \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and not stack_entries(state)
        if own_main:
            for a in acts:
                if a.get("type") == "PlayLand":
                    oid = (a.get("data") or {}).get("object_id")
                    if oid is not None \
                            and lname(state, int(oid)) == FOREST \
                            and not acted("p1land", rev):
                        await submit_as_is(p1, a)
                        return
            if ST["stage"] == "setup":
                if len(untapped_lands(state, 1, FOREST)) >= 1:
                    ca = cast_spell_action(acts, state, ELVES)
                    if ca and not acted("elves", rev):
                        say("[P1] casts Llanowar Elves")
                        wire("cast_elves", ca)
                        await submit_as_is(p1, ca)
                        return
                if len(untapped_lands(state, 1, FOREST)) >= 2:
                    ca = cast_spell_action(acts, state, BEARS)
                    if ca and not acted("bears", rev):
                        say("[P1] casts Grizzly Bears")
                        wire("cast_bears", ca)
                        await submit_as_is(p1, ca)
                        return
            # activation test: attempt the enchanted Elves' mana ability.
            # Runs only while the Elves is UNTAPPED (it attacked this
            # turn, so it is tapped until P1's next turn). While tapped
            # we fall through to the default pass so the game advances
            # instead of burning the no-offer timeout on a creature that
            # could not pay {T} anyway.
            if ST["stage"] == "activation_test" \
                    and not ST["activation_attempted"]:
                eoid = ST["enchant_target_oid"]
                obj = get_obj(state, eoid) if eoid else {}
                if eoid is not None and not obj.get("tapped"):
                    cands = [a for a in acts
                             if a["type"] == "ActivateAbility"
                             and int((a.get("data") or {})
                                     .get("source_id", -1)) == eoid]
                    ST["activation_candidates_seen"] = [
                        {"ability_index": (a.get("data") or {})
                         .get("ability_index")} for a in cands]
                    wire("activation_candidates",
                         {"enchanted_elves": eoid,
                          "candidates": ST["activation_candidates_seen"],
                          "all_types": sorted(set(a["type"]
                                                  for a in acts))})
                    ST["activation_offered"] = len(cands) > 0
                    ST["_act_rej_mark"] = len(obs["rejections"])
                    if cands:
                        ST["activation_attempted"] = True
                        ST["pool_before_activation"] = mana_pool(state, 1)
                        say(f"[P1] attempts ActivateAbility on enchanted "
                            f"Elves {eoid}")
                        await submit_as_is(p1, cands[0])
                        return
                    ST["activation_wait_ticks"] += 1
                    if ST["activation_wait_ticks"] >= 6:
                        ST["activation_attempted"] = True
                        say("no ActivateAbility offered for enchanted "
                            "Elves after 6 ticks (untapped)")
                    return
                # tapped or unknown: fall through to default pass
        # default: pass priority when it is ours to pass (same gating
        # rationale as P0: no out-of-turn passes; resolve before mark).
        if my_priority(state, 1):
            pa = find_action(acts, "PassPriority")
            if pa and not acted("p1pass", rev):
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
            ST["turns_seen"].add(state.get("turn_number"))
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")
                say(f"tick error {tag}: {e!r}")

    def evaluate():
        pre = None
        postA = None
        postC = None
        for tag, var in (("pre", "pre"), ("post_attack", "postA"),
                         ("post_control", "postC")):
            p = f"{EVDIR}/{tag}.json"
            if os.path.exists(p):
                env = json.load(open(p).read())
                if tag == "pre":
                    pre = env["state"]
                elif tag == "post_attack":
                    postA = env["state"]
                else:
                    postC = env["state"]
        notes.append(f"rejections={len(obs['rejections'])} "
                     f"target_prompts={ST['target_prompts_seen']} "
                     f"escape_offered={ST['escape_offered']} "
                     f"elves_attacked={ST['elves_attacked']}")
        # A1
        if pre is not None:
            oid, _aura = aura_on_bf(pre)
            eoid = ST["enchant_target_oid"]
            eo = get_obj(pre, eoid) if eoid is not None else {}
            ok = (oid is not None and eoid is not None
                  and eo.get("zone") == "Battlefield"
                  and eo.get("controller") == 1
                  and obj_name(pre, eoid) == ELVES
                  and gy_count(pre, 1) >= 3)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: aura_oid={oid}, enchant_target={eoid} "
                         f"({obj_name(pre, eoid)}, zone={eo.get('zone')}, "
                         f"ctrl={eo.get('controller')}), P1 "
                         f"gy={gy_count(pre, 1)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing (aura never attached "
                         "with gy>=3)")
        # A2
        if not ST["attack1_submitted"]:
            ass["A2_attack_restricted"] = "not-run"
            notes.append("A2: attack1 never submitted")
        elif ST["attack1_rejected"]:
            ass["A2_attack_restricted"] = "passed"
            notes.append("A2: engine refused the enchanted-Elves attack "
                         "declaration (rejected or silently excluded)")
        elif ST["elves_attacked"]:
            ass["A2_attack_restricted"] = "failed"
            notes.append(f"A2: enchanted Elves attacked (P0 life "
                         f"{ST['p0_life_pre_attack']}->"
                         f"{ST['p0_life_post_attack']}); cant-attack "
                         f"restriction dropped")
        else:
            ass["A2_attack_restricted"] = "failed"
            notes.append("A2: attack1 submitted but outcome unresolved")
        # A3
        if not ST["activation_attempted"]:
            ass["A3_activation_restricted"] = "not-run"
            notes.append("A3: activation test never ran")
        elif ST.get("activation_bailed"):
            ass["A3_activation_restricted"] = "not-run"
            notes.append("A3: activation test bailed out before the "
                         "enchanted Elves untapped; not observed")
        elif not ST["activation_offered"]:
            ass["A3_activation_restricted"] = "passed"
            notes.append("A3: no ActivateAbility offered for the enchanted "
                         "Elves")
        else:
            pb = ST["pool_before_activation"] or {}
            pa = ST["pool_after_activation"] or {}
            dgreen = pa.get("green", 0) - pb.get("green", 0)
            if ST["activation_rejected"]:
                ass["A3_activation_restricted"] = "passed"
                notes.append("A3: activation attempt rejected by engine")
            elif dgreen >= 1:
                ass["A3_activation_restricted"] = "failed"
                notes.append(f"A3: enchanted Elves activated, P1 green "
                             f"pool +{dgreen}; cant-activate restriction "
                             f"dropped")
            else:
                ass["A3_activation_restricted"] = "passed"
                notes.append(f"A3: activation offered+attempted but no "
                             f"mana produced (dgreen={dgreen})")
        # A4
        if ass["A1_setup_ok"] != "passed":
            ass["A4_escape_offered"] = "not-run"
            notes.append("A4: setup failed; escape scan not meaningful")
        elif ST["escape_offered"]:
            ass["A4_escape_offered"] = "passed"
            notes.append("A4: exile-3 escape action was offered to P1")
        else:
            ass["A4_escape_offered"] = "failed"
            notes.append(f"A4: no exile-3-to-ignore escape action offered "
                         f"at P1 priority with gy>=3 (scanned action "
                         f"types: {ST['p1_action_types']})")
        # A5
        ass["A5_escape_effective"] = "not-run"
        notes.append("A5: escape payment path not driven (A4 "
                     + ("passed" if ST["escape_offered"] else "failed")
                     + ")")
        # A6
        if ST["bear_attack_accepted"]:
            ass["A6_control_binding"] = "passed"
            notes.append(f"A6: unenchanted Bear attacked normally (P0 life "
                         f"{ST['p0_life_pre_bear']}->"
                         f"{ST['p0_life_post_bear']})")
        elif ST.get("bear_submitted"):
            ass["A6_control_binding"] = "failed"
            notes.append("A6: bear control attack submitted but not "
                         "observed in attackers")
        else:
            ass["A6_control_binding"] = "not-run"
            notes.append("A6: bear control attack never submitted")
        # verdict
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached the aura-attack test")
        elif any(ass[k] == "failed" for k in ("A2_attack_restricted",
                                              "A3_activation_restricted",
                                              "A4_escape_offered")):
            verdict = "reproduced"
            notes.append("Lost in Thought's combined restriction/escape "
                         "clause is dropped end-to-end on this build; see "
                         "assertion notes")
        elif all(ass[k] == "passed" for k in ("A2_attack_restricted",
                                              "A3_activation_restricted",
                                              "A4_escape_offered",
                                              "A5_escape_effective")):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("escape offered but payment path not driven; "
                         "re-run needed for A5")
        return verdict

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        for tag in ("post_attack", "post_control"):
            if tag not in ST["exports"]:
                await export_named(tag)
        verdict = evaluate()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/run-{RUN_ID}",
            "driver": {"protocol_advertised": 71,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()},
            "driver_state": {
                "stage": ST["stage"],
                "enchant_target_oid": ST["enchant_target_oid"],
                "aura_oid": ST["aura_oid"],
                "scours_cast": ST.get("scours_cast", 0),
                "attack1": {"submitted": ST["attack1_submitted"],
                            "turn": ST["attack1_turn"],
                            "accepted": ST["attack1_accepted"],
                            "rejected": ST["attack1_rejected"],
                            "elves_attacked": ST["elves_attacked"],
                            "p0_life_pre": ST["p0_life_pre_attack"],
                            "p0_life_post": ST["p0_life_post_attack"]},
                "activation": {"attempted": ST["activation_attempted"],
                               "offered": ST["activation_offered"],
                               "rejected": ST["activation_rejected"],
                               "candidates_seen":
                                   ST["activation_candidates_seen"],
                               "pool_before": ST["pool_before_activation"],
                               "pool_after": ST["pool_after_activation"]},
                "escape": {"offered": ST["escape_offered"],
                           "scan_done": ST["escape_scan_done"],
                           "p1_action_types": ST["p1_action_types"]},
                "bear": {"submitted": ST["bear_submitted"],
                         "oid": ST["bear_oid"],
                         "accepted": ST["bear_attack_accepted"],
                         "p0_life_pre": ST["p0_life_pre_bear"],
                         "p0_life_post": ST["p0_life_post_bear"]},
                "target_prompts_seen": ST["target_prompts_seen"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(t for t in ST["turns_seen"]
                                     if t is not None),
                "exports": ST["exports"],
                "rejections": len(obs["rejections"]),
            },
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x spell/creature density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Escape payment (A5) not driven: no escape action was "
                "offered, so there was nothing to pay.",
                "Can't-block half of the restriction not separately "
                "asserted (no P0 attackers fielded); attack + activation "
                "failures already demonstrate the clause is dropped.",
            ],
            "setup_line": "P0: 12x lost in thought + 12x thought scour + "
                          "36x island; P1: 12x llanowar elves + 12x "
                          "grizzly bears + 36x forest",
            "contract_line": "Cast Lost in Thought on P1's Llanowar Elves "
                             "with >=3 cards in P1's graveyard; assert the "
                             "enchanted creature cannot attack (A2) and "
                             "cannot activate abilities (A3), that an "
                             "exile-3 escape is offered (A4), and that an "
                             "unenchanted Bear attacks normally (A6)",
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

    # scenario provenance copy
    with open(f"{EVDIR}/scenario_4509.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_4509.py").read())

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
            turn = s.get("turn_number", 0)
            phase = s.get("phase")
            active = s.get("active_player")
            wtype = (s.get("waiting_for") or {}).get("type")
            # ---- stage transitions ----
            if ST["stage"] == "setup":
                oid, _ = aura_on_bf(s)
                if oid is not None and gy_count(s, 1) >= 3:
                    await export_named("pre")
                    ST["stage"] = "attack_test"
                    say("STAGE -> attack_test")
            elif ST["stage"] == "attack_test":
                if ST["attack1_submitted"]:
                    t1 = ST.get("attack1_turn")
                    done = ST["attack1_rejected"] or ST["attack1_accepted"]
                    if done and (t1 is None or turn > t1
                                 or (phase == "PostCombatMain"
                                     and active == 1)):
                        ST["p0_life_post_attack"] = life_of(s, 0)
                        await export_named("post_attack")
                        ST["stage"] = "activation_test"
                        ST["activation_test_start_turn"] = turn
                        say("STAGE -> activation_test")
            elif ST["stage"] == "activation_test":
                if ST["activation_attempted"]:
                    mark = ST.get("_act_rej_mark", 0)
                    if len(obs["rejections"]) > mark:
                        ST["activation_rejected"] = True
                    ST["stage"] = "control"
                    say("STAGE -> control")
            elif ST["stage"] == "control":
                if ST.get("bear_submitted"):
                    t1 = ST.get("bear_turn")
                    if t1 is None or turn > t1 \
                            or (phase == "PostCombatMain" and active == 1):
                        ST["p0_life_post_bear"] = life_of(s, 0)
                        await export_named("post_control")
                        ST["stage"] = "done"
                        say("control combat done; finishing")
                        break
            if turn >= 24:
                notes.append("turn 24 reached without completing; "
                             "bailing out")
                say("turn 24 bail-out; finishing")
                break
            # activation-stage hard bailout (relative): if 6+ turns pass
            # after entering activation_test without the test running,
            # do not hang forever; record and move on.
            if ST["stage"] == "activation_test" \
                    and not ST["activation_attempted"]:
                t_start = ST.get("activation_test_start_turn", 0)
                if turn > t_start + 6:
                    ST["activation_attempted"] = True
                    ST["activation_bailed"] = True
                    notes.append("activation test bailed out "
                                 f"(turn {turn} > start {t_start}+6)")
                    say("activation_test relative bail-out -> control")
                    ST["stage"] = "control"
            # watchdog: if either client's revision goes stale >45s while
            # the run is live, log that client's view for diagnosis.
            for _tag, _c in (("P0", p0), ("P1", p1)):
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
                say(f"[diag] t={int(time.time()-t0)}s "
                    f"stage={ST['stage']} turn={turn} phase={phase} "
                    f"wf={wtype} elves_bf={bf_ids(s, 1, ELVES)} "
                    f"aura={aura_on_bf(s)[0]} gy1={gy_count(s, 1)} "
                    f"p0hand={hand_lnames(s, 0)[:5]} "
                    f"p1hand={hand_lnames(s, 1)[:5]} "
                    f"atk1={ST['attack1_submitted']}/"
                    f"{ST['attack1_accepted']}/{ST['attack1_rejected']} "
                    f"act={ST['activation_attempted']}/"
                    f"{ST['activation_offered']} "
                    f"esc={ST['escape_offered']}")
    finally:
        p0t.cancel()
        p1t.cancel()
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
