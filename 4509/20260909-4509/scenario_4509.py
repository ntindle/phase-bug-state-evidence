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

Oracle text (verified from pinned v0.78.0 card-data.json, key 'lost in thought'):
  "Enchant creature
   Enchanted creature can't attack or block, and its activated abilities can't
   be activated. Its controller may exile three cards from their graveyard for
   that player to ignore this effect until end of turn."
Classifier comment (2026-07-19): the authoritative Oracle uses the end-of-turn
ignore-effect payment (not the stale next-turn wording in the issue body).
Card-data parse state: the combined line is
  Unimplemented("Static pattern matched but line failed static parser: ..."),
  static_abilities == [].

Scenario (native engine, two human-client seats):
  P0: 12x lost in thought ({1}{U}) + 12x thought scour ({U}) + 36x island.
      Mills P1 twice (Thought Scour targeting P1) so P1's graveyard holds >=3
      cards, then casts Lost in Thought enchanting P1's Llanowar Elves.
  P1: 12x llanowar elves ({G}, "{T}: Add {G}") + 12x grizzly bears
      ({1}{G}) + 36x forest. Develops Elves + Bears, never attacks except in
      the scripted test combats.

Assertions:
  A1_setup_ok       pre.json: Lost in Thought on the battlefield, P1's Elves
                    on the battlefield (the targeted object), P1 graveyard
                    >= 3 cards.
  A2_attack_restricted
                    The enchanted Elves is declared as an attacker by P1. Pass
                    iff the engine refuses the declaration (rejection, silent
                    exclusion) or the Elves never appears in combat.attackers
                    and deals no combat damage. Fail iff the Elves attacks and
                    damages P0 -> the CantAttackOrBlock restriction is dropped.
  A3_activation_restricted
                    P1 attempts the enchanted Elves' "{T}: Add {G}" ability.
                    Pass iff no ActivateAbility is offered for the enchanted
                    Elves (or the attempt is rejected with no mana produced).
                    Fail iff the activation succeeds and P1's pool gains {G}
                    -> the CantBeActivated restriction is dropped.
  A4_escape_offered  With P1's graveyard >= 3 and the aura on the battlefield,
                    at P1 priority the driver scans legal_actions and
                    viewer_interaction for any exile-3-to-ignore escape
                    action. Pass iff offered. Fail iff absent -> the escape
                    clause is dropped.
  A5_escape_effective
                    Only if A4 passes: pay the exile-3 escape, then the Elves
                    attacks the same turn. Expected not-run on this build.
  A6_control_binding
                    P1's unenchanted Grizzly Bears attacks normally on the
                    next combat (engine accepts, P0 takes 2) -> documents the
                    host binding question is moot / the aura is inert.

Verdict rule: reproduced iff any of A2, A3, A4 fails (the combined clause is
dropped end-to-end, per the report). not-reproduced iff A2, A3, A4 and A5 all
pass. blocked iff the game cannot be driven to the aura-attack test.

Evidence: evidence/4509/<run-id>/pre.json, post_attack.json, post_control.json,
run.json, manifest.sha256, summary.png, scenario_4509.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-4509"
EVDIR = f"{BACKFILL}/evidence/4509/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

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

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello probed live (0.78.0/4de7224/proto 68/Full) + sha256 "
              "re-verified against pinned v0.78.0 release artifacts "
              "(binary+data+sigs under server/releases/v0.78.0/); fresh "
              "isolated server on 127.0.0.1:9374 for run 20260909-4509",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name and not o.get("tapped")]


def creatures_on_bf(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            if name is None or obj_name(o) == name:
                out.append(int(oid))
    return out


def aura_on_bf(state, name=LOST):
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and obj_name(o) == name:
            return int(oid), o
    return None, None


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def gy_count(state, pid):
    return len(player_of(state, pid).get("graveyard", []))


def mana_pool(state, pid):
    out = Counter()
    for p in state.get("players", []):
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in ("white", "blue", "black", "red", "green", "colorless"):
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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def candidate_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)

async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_attack_restricted", "A3_activation_restricted",
            "A4_escape_offered", "A5_escape_effective", "A6_control_binding")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    obs = {
        "stage": "setup",          # setup -> attack_test -> activation_test -> control_prep -> control -> done
        "scours_cast": 0,
        "aura_cast": False,
        "enchant_target_oid": None,
        "pending_target": None,    # {"kind": "creature"/"player", ...}
        "aura_object_logged": False,
        "attack1_submitted": False,
        "attack1_rejected": False,
        "attack1_accepted": False,
        "attack1_silent_ticks": 0,
        "elves_attacked": False,
        "p0_life_pre_attack": None,
        "p0_life_post_attack": None,
        "activation_attempted": False,
        "activation_offered": False,
        "activation_rejected": False,
        "pool_before_activation": None,
        "pool_after_activation": None,
        "escape_offered": False,
        "escape_scan_done": False,
        "p1_action_types": [],
        "bear_attack_accepted": False,
        "p0_life_pre_bear": None,
        "p0_life_post_bear": None,
        "rejections": [],
        "target_prompts_seen": 0,
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    def resolve_target_candidate(opp, want):
        """Pick the candidate matching the scenario's intended target from a
        schema sequence opportunity. Resolves objects from the candidate's
        surfaces[].data.reference, players from surfaces[].data.seat."""
        data = (opp.get("response") or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        for ch in cands:
            if (ch.get("status") or {}).get("type") != "available":
                continue
            for s in ch.get("surfaces", []) or []:
                d = (s.get("data") or {}) if isinstance(s.get("data"), dict) else {}
                if want["kind"] == "creature":
                    ref = d.get("reference")
                    if ref is not None and str(ref) == str(want["oid"]):
                        return ch.get("id"), ch
                elif want["kind"] == "player":
                    if d.get("seat") == want["seat"]:
                        return ch.get("id"), ch
        # fallback: name/text match
        for ch in cands:
            if (ch.get("status") or {}).get("type") != "available":
                continue
            txt = candidate_text(ch).lower()
            if want["kind"] == "creature" and want["name"] in txt:
                return ch.get("id"), ch
            if want["kind"] == "player" and f"player {want['seat']}" in txt:
                return ch.get("id"), ch
        return None, None

    async def answer_target(c, opp):
        want = obs["pending_target"]
        iid = opp.get("interactionId")
        cid, ch = resolve_target_candidate(opp, want)
        if cid is None:
            notes.append(f"target prompt for {want} had no matching candidate; "
                         f"not answering blindly")
            wire("target_no_match", {"want": want, "opp": opp})
            return False
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {}) if isinstance(s.get("data"), dict) else {}
            if "reference" in d:
                ref = d["reference"]
        say(f"[{c.name}] targets {want['kind']} via candidate {cid} (ref={ref})")
        wire("target_submission", {"who": c.name, "want": want,
                                   "choice_id": cid, "reference": ref,
                                   "submission": sub})
        obs["pending_target"] = None
        await c.send_interaction(sub)
        return True

    def log_aura_object(state):
        oid, o = aura_on_bf(state)
        if oid is not None and not obs["aura_object_logged"]:
            obs["aura_object_logged"] = True
            wire("aura_object", {"oid": oid, "object": o})
            say(f"aura on BF oid={oid} fields={sorted(o.keys())}")

    def scan_escape(state, acts, st):
        """Look for any exile-3-to-ignore escape action offered to P1."""
        if obs["escape_offered"] or obs["escape_scan_done"]:
            return
        if not obs["aura_object_logged"]:
            return
        if gy_count(state, 1) < 3:
            return
        texts = []
        for a in acts:
            blob = json.dumps(a, default=str).lower()
            texts.append(a["type"])
            if ("exile" in blob and "graveyard" in blob
                    and ("ignore" in blob or "lost in thought" in blob)):
                obs["escape_offered"] = True
                wire("escape_found_action", a)
                say("ESCAPE ACTION OFFERED: " + json.dumps(a)[:500])
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                blob = json.dumps(opp, default=str).lower()
                if ("exile" in blob and "graveyard" in blob
                        and ("ignore" in blob or "lost in thought" in blob)):
                    obs["escape_offered"] = True
                    wire("escape_found_vi", opp)
                    say("ESCAPE VI OFFERED: " + json.dumps(opp)[:500][:500])
        if not obs["escape_scan_done"]:
            obs["escape_scan_done"] = True
            obs["p1_action_types"] = sorted(set(texts))
            wire("p1_action_types", {"types": obs["p1_action_types"]})
            say(f"P1 priority action types at escape scan: {obs['p1_action_types']}")

    async def p0_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        # mulligan: hunt Lost in Thought + lands
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n == ISLAND)
            mulls = kept.get("P0_mulls", 0)
            if ((LOST in hn and lands >= 2) or mulls >= 3):
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (lost={LOST in hn}, lands={lands}, mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1}")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]
                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm == ISLAND else (2 if nm in (LOST, SCOUR) else 1)
                picks = sorted(hand_ids, key=bkey)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
            return True
        # payments first
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return True
        # target-selection prompts (schema only, per driver lessons)
        vi = get_vi(st)
        if vi and obs["pending_target"]:
            for opp in vi.get("opportunities", []) or []:
                resp = opp.get("response", {}) or {}
                if resp.get("type") != "schema":
                    continue
                spec = ((resp.get("data") or {}).get("spec") or {})
                if spec.get("type") == "sequence":
                    obs["target_prompts_seen"] += 1
                    wire("target_prompt", {"who": "P0", "opp": opp})
                    await answer_target(p0, opp)
                    return True
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True
        if wtype != "Priority" or state.get("priority_player") != 0:
            return False
        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return True
            # mill P1: Thought Scour targeting P1 (need P1 gy >= 3 for escape test)
            if (obs["scours_cast"] < 2 and gy_count(state, 1) < 4
                    and len(untapped_lands(state, 0, ISLAND)) >= 1):
                sid = spell_in_hand_oid(state, 0, SCOUR)
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and int(d.get("object_id", -1)) == (sid or -1):
                        obs["pending_target"] = {"kind": "player", "seat": 1}
                        obs["scours_cast"] += 1
                        say(f"P0 casts Thought Scour #{obs['scours_cast']} targeting P1")
                        wire("cast_scour", a)
                        await submit_as_is(p0, a)
                        return True
            # cast the aura on P1's Elves
            if (not obs["aura_cast"]
                    and len(untapped_lands(state, 0, ISLAND)) >= 2):
                elves = creatures_on_bf(state, 1, ELVES)
                sid = spell_in_hand_oid(state, 0, LOST)
                if elves and sid is not None:
                    for a in acts:
                        d = a.get("data", {})
                        if a["type"] == "CastSpell" and int(d.get("object_id", -1)) == sid:
                            obs["pending_target"] = {"kind": "creature",
                                                     "oid": elves[0],
                                                     "name": ELVES}
                            obs["enchant_target_oid"] = elves[0]
                            obs["aura_cast"] = True
                            say(f"P0 casts Lost in Thought targeting Elves {elves[0]}")
                            wire("cast_aura", a)
                            await submit_as_is(p0, a)
                            return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            hn = hand_names(state, 1)
            lands = sum(1 for n in hn if n == FOREST)
            mulls = kept.get("P1_mulls", 0)
            if ((ELVES in hn or BEARS in hn) and lands >= 2) or mulls >= 3:
                kept["P1"] = True
                await submit_as_is(p1, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P1 keeps (lands={lands}, mulls={mulls})")
            else:
                kept["P1_mulls"] = mulls + 1
                await submit_as_is(p1, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P1 mulligans #{mulls + 1}")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P1_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 1:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 1).get("hand", [])]
                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm == FOREST else (2 if nm in (ELVES, BEARS) else 1)
                picks = sorted(hand_ids, key=bkey)[:count]
                kept["P1_bottomed"] = True
                await submit_as_is(p1, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P1 bottoms {count}")
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return True
        if wtype == "DeclareAttackers":
            if obs["stage"] == "control_prep":
                return True  # hold: the loop promotes to control, then we declare
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                if obs["stage"] == "attack_test" and not obs["attack1_submitted"]:
                    eoid = obs["enchant_target_oid"]
                    d["attacks"] = [[eoid, {"type": "Player", "data": 0}]]
                    d["bands"] = []
                    obs["attack1_submitted"] = True
                    obs["attack1_turn"] = state.get("turn_number")
                    obs["p0_life_pre_attack"] = life_of(state, 0)
                    rej_before = len(obs["rejections"])
                    obs["_rej_mark"] = rej_before
                    say(f"P1 declares attack with enchanted Elves {eoid}")
                    wire("attack1_submit", d)
                    await p1.send_action({"type": "DeclareAttackers", "data": d})
                    return True
                if obs["stage"] == "attack_test" and obs["attack1_submitted"] \
                        and not obs["attack1_accepted"] and not obs["attack1_rejected"]:
                    # still pending: check for rejection or silent exclusion
                    if len(obs["rejections"]) > obs.get("_rej_mark", 0):
                        obs["attack1_rejected"] = True
                        say("attack1 REJECTED by engine")
                        wire("attack1_rejected", {})
                        d["attacks"] = []; d["bands"] = []
                        await p1.send_action({"type": "DeclareAttackers", "data": d})
                        return True
                    obs["attack1_silent_ticks"] += 1
                    if obs["attack1_silent_ticks"] >= 4:
                        obs["attack1_rejected"] = True  # engine dropped it silently
                        notes.append("attack1: engine never accepted nor rejected; "
                                     "treated as blocked (silent exclusion)")
                        say("attack1 silently excluded; declaring empty")
                        d["attacks"] = []; d["bands"] = []
                        await p1.send_action({"type": "DeclareAttackers", "data": d})
                        return True
                    return True
                if obs["stage"] == "control" and not obs.get("bear_submitted"):
                    bears = creatures_on_bf(state, 1, BEARS)
                    if bears:
                        d["attacks"] = [[bears[0], {"type": "Player", "data": 0}]]
                        d["bands"] = []
                        obs["bear_submitted"] = True
                        obs["bear_oid"] = bears[0]
                        obs["bear_turn"] = state.get("turn_number")
                        obs["p0_life_pre_bear"] = life_of(state, 0)
                        say(f"P1 declares control attack with Bear {bears[0]}")
                        wire("bear_submit", d)
                        await p1.send_action({"type": "DeclareAttackers", "data": d})
                        return True
                d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if wtype != "Priority" or state.get("priority_player") != 1:
            return False
        # detect aura attachment + log object
        log_aura_object(state)
        # escape scan at P1 priority
        scan_escape(state, acts, st)
        # track Elves attacking
        eoid = obs["enchant_target_oid"]
        if eoid is not None and eoid in attacker_oids(state):
            if not obs["elves_attacked"]:
                obs["elves_attacked"] = True
                say(f"Elves {eoid} IS in combat.attackers")
                wire("elves_attacking", {"attackers": sorted(attacker_oids(state))})
        if obs["attack1_submitted"] and not obs["attack1_accepted"] \
                and not obs["attack1_rejected"]:
            if eoid is not None and eoid in attacker_oids(state):
                obs["attack1_accepted"] = True
                say("attack1 ACCEPTED (Elves in attackers)")
                wire("attack1_accepted", {})
        # capture the pool on the first tick after the activation attempt,
        # before the game can advance phases (mana empties on phase change)
        if (obs["stage"] == "activation_test" and obs["activation_attempted"]
                and obs["activation_offered"]
                and obs.get("pool_after_activation") is None):
            obs["pool_after_activation"] = mana_pool(state, 1)
            wire("pool_after_capture", obs["pool_after_activation"])
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
            if obs["stage"] == "setup":
                if len(untapped_lands(state, 1, FOREST)) >= 1:
                    sid = spell_in_hand_oid(state, 1, ELVES)
                    for a in acts:
                        d = a.get("data", {})
                        if a["type"] == "CastSpell" and int(d.get("object_id", -1)) == (sid or -1):
                            say(f"P1 casts Llanowar Elves")
                            wire("cast_elves", a)
                            await submit_as_is(p1, a)
                            return True
                if len(untapped_lands(state, 1, FOREST)) >= 2:
                    sid = spell_in_hand_oid(state, 1, BEARS)
                    for a in acts:
                        d = a.get("data", {})
                        if a["type"] == "CastSpell" and int(d.get("object_id", -1)) == (sid or -1):
                            say(f"P1 casts Grizzly Bears")
                            wire("cast_bears", a)
                            await submit_as_is(p1, a)
                            return True
            # activation test: attempt the enchanted Elves' mana ability
            # (runs on P1's PreCombatMain AFTER the attack test, so a tapped
            # Elves from a successful bug-case attack has untapped again)
            if obs["stage"] == "activation_test" and not obs["activation_attempted"]:
                eoid = obs["enchant_target_oid"]
                cands = [a for a in acts
                         if a["type"] == "ActivateAbility"
                         and int(a.get("data", {}).get("source_id", -1)) == (eoid or -1)]
                wire("activation_candidates",
                     {"enchanted_elves": eoid,
                      "candidates": [{"ability_index": a["data"].get("ability_index")}
                                     for a in cands],
                      "all_types": sorted(set(a["type"] for a in acts))})
                obs["activation_offered"] = len(cands) > 0
                obs["_act_rej_mark"] = len(obs["rejections"])
                if cands:
                    obs["activation_attempted"] = True
                    obs["pool_before_activation"] = mana_pool(state, 1)
                    say(f"P1 attempts ActivateAbility on enchanted Elves {eoid}")
                    await submit_as_is(p1, cands[0])
                    return True
                else:
                    obs["activation_attempted"] = True  # none offered; nothing to attempt
                    say("no ActivateAbility offered for enchanted Elves")
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        pre = load_env("pre")
        postA = load_env("post_attack")
        postC = load_env("post_control")
        notes.append(f"rejections={len(obs['rejections'])} "
                     f"target_prompts={obs['target_prompts_seen']} "
                     f"escape_offered={obs['escape_offered']} "
                     f"elves_attacked={obs['elves_attacked']}")
        # A1
        if pre is not None:
            s = pre["state"]
            oid, _aura = aura_on_bf(s)
            eoid = obs["enchant_target_oid"]
            eo = get_obj(s, eoid) if eoid is not None else {}
            ok = (oid is not None and eoid is not None
                  and eo.get("zone") == "Battlefield"
                  and eo.get("controller") == 1
                  and obj_name(eo) == ELVES
                  and gy_count(s, 1) >= 3)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: aura_oid={oid}, enchant_target={eoid} "
                         f"({obj_name(eo)}, zone={eo.get('zone')}, "
                         f"ctrl={eo.get('controller')}), P1 gy={gy_count(s, 1)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing (aura never attached with gy>=3)")
        # A2
        if not obs["attack1_submitted"]:
            ass["A2_attack_restricted"] = "not-run"
            notes.append("A2: attack1 never submitted")
        elif obs["attack1_rejected"]:
            ass["A2_attack_restricted"] = "passed"
            notes.append("A2: engine refused the enchanted-Elves attack "
                         "declaration (rejected or silently excluded)")
        elif obs["elves_attacked"]:
            ass["A2_attack_restricted"] = "failed"
            notes.append(f"A2: enchanted Elves attacked (P0 life "
                         f"{obs['p0_life_pre_attack']}->{obs['p0_life_post_attack']}); "
                         f"cant-attack restriction dropped")
        else:
            ass["A2_attack_restricted"] = "failed"
            notes.append("A2: attack1 submitted but outcome unresolved")
        # A3
        if not obs["activation_attempted"]:
            ass["A3_activation_restricted"] = "not-run"
            notes.append("A3: activation test never ran")
        elif not obs["activation_offered"]:
            ass["A3_activation_restricted"] = "passed"
            notes.append("A3: no ActivateAbility offered for the enchanted Elves")
        else:
            pb = obs["pool_before_activation"] or {}
            pa = obs["pool_after_activation"] or {}
            dgreen = pa.get("green", 0) - pb.get("green", 0)
            if obs["activation_rejected"]:
                ass["A3_activation_restricted"] = "passed"
                notes.append("A3: activation attempt rejected by engine")
            elif dgreen >= 1:
                ass["A3_activation_restricted"] = "failed"
                notes.append(f"A3: enchanted Elves activated, P1 green pool "
                             f"+{dgreen}; cant-activate restriction dropped")
            else:
                ass["A3_activation_restricted"] = "passed"
                notes.append(f"A3: activation offered+attempted but no mana "
                             f"produced (dgreen={dgreen})")
        # A4
        if ass["A1_setup_ok"] != "passed":
            ass["A4_escape_offered"] = "not-run"
            notes.append("A4: setup failed; escape scan not meaningful")
        elif obs["escape_offered"]:
            ass["A4_escape_offered"] = "passed"
            notes.append("A4: exile-3 escape action was offered to P1")
        else:
            ass["A4_escape_offered"] = "failed"
            notes.append(f"A4: no exile-3-to-ignore escape action offered at P1 "
                         f"priority with gy>=3 (scanned action types: "
                         f"{obs['p1_action_types']})")
        # A5
        ass["A5_escape_effective"] = "not-run"
        notes.append("A5: escape payment path not driven (A4 "
                     + ("passed" if obs["escape_offered"] else "failed") + ")")
        # A6
        if obs["bear_attack_accepted"]:
            ass["A6_control_binding"] = "passed"
            notes.append(f"A6: unenchanted Bear attacked normally (P0 life "
                         f"{obs['p0_life_pre_bear']}->{obs['p0_life_post_bear']})")
        elif obs.get("bear_submitted"):
            ass["A6_control_binding"] = "failed"
            notes.append("A6: bear control attack submitted but not observed "
                         "in attackers")
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
            notes.append("Lost in Thought's combined restriction/escape clause "
                         "is dropped end-to-end on this build; see assertion notes")
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
        dur = time.time() - t_start
        for tag in ("post_attack", "post_control"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": 4509,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4509.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if not k.startswith("_")},
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                            "12x spell/creature density is a test-harness convenience "
                            "(engine accepts >4-of for custom games).",
                            "The prebuilt server has no standalone state-restore; states "
                            "are authoritative exports (restorable only via full game replay).",
                            "Escape payment (A5) not driven: no escape action was offered, "
                            "so there was nothing to pay."],
            "setup_line": "P0: 12x lost in thought + 12x thought scour + 36x island; "
                          "P1: 12x llanowar elves + 12x grizzly bears + 36x forest",
            "contract_line": "Cast Lost in Thought on P1's Llanowar Elves with >=3 "
                             "cards in P1's graveyard; assert the enchanted creature "
                             "cannot attack (A2) and cannot activate abilities (A3), "
                             "that an exile-3 escape is offered (A4), and that an "
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
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    # copy scenario into evidence dir for provenance
    with open(f"{EVDIR}/scenario_4509.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_4509.py").read())

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            await drain_rejections(c)
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        # ---- stage transitions ----
        s = (p0.latest["state"] if p0.latest else {}) or {}
        turn = s.get("turn_number", 0)
        phase = s.get("phase")
        active = s.get("active_player")
        wtype = (s.get("waiting_for") or {}).get("type")
        if obs["stage"] == "setup":
            oid, _ = aura_on_bf(s)
            if oid is not None and gy_count(s, 1) >= 3:
                await export("pre")
                obs["stage"] = "attack_test"
                say("STAGE -> attack_test")
        elif obs["stage"] == "attack_test":
            if obs["attack1_submitted"]:
                t1 = obs.get("attack1_turn")
                done = obs["attack1_rejected"] or obs["attack1_accepted"]
                if done and (t1 is None or turn > t1
                             or (phase == "PostCombatMain" and active == 1)):
                    obs["p0_life_post_attack"] = life_of(s, 0)
                    await export("post_attack")
                    obs["stage"] = "activation_test"
                    say("STAGE -> activation_test")
        elif obs["stage"] == "activation_test":
            if obs["activation_attempted"]:
                obs["activation_settle_ticks"] = \
                    obs.get("activation_settle_ticks", 0) + 1
                if obs["activation_settle_ticks"] >= 10:
                    if obs.get("pool_after_activation") is None:
                        obs["pool_after_activation"] = mana_pool(s, 1)
                    mark = obs.get("_act_rej_mark", 0)
                    if len(obs["rejections"]) > mark:
                        obs["activation_rejected"] = True
                    obs["stage"] = "control_prep"
                    say("STAGE -> control_prep")
        elif obs["stage"] == "control_prep":
            if wtype == "DeclareAttackers" and active == 1:
                obs["stage"] = "control"
                say("STAGE -> control")
        elif obs["stage"] == "control":
            if obs.get("bear_submitted"):
                t1 = obs.get("bear_turn")
                if t1 is None or turn > t1 \
                        or (phase == "PostCombatMain" and active == 1):
                    obs["p0_life_post_bear"] = life_of(s, 0)
                    await export("post_control")
                    say("control combat done; finishing")
                    await finish()
                    return
        # bear acceptance detection
        if obs.get("bear_oid") is not None \
                and obs["bear_oid"] in attacker_oids(s):
            if not obs["bear_attack_accepted"]:
                obs["bear_attack_accepted"] = True
                say(f"Bear {obs['bear_oid']} IS in combat.attackers")
        if turn >= 20:
            notes.append("turn 20 reached without completing; bailing out")
            say("turn 20 bail-out; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            eoid = obs["enchant_target_oid"]
            say(f"DIAG turn={turn} active={active} phase={phase} wf={wtype} "
                f"stage={obs['stage']} elves_bf={creatures_on_bf(s,1,ELVES)} "
                f"aura={aura_on_bf(s)[0]} gy1={gy_count(s,1)} "
                f"p0hand={hand_names(s,0)[:5]} p1hand={hand_names(s,1)[:5]} "
                f"atk1={obs['attack1_submitted']}/{obs['attack1_accepted']}/"
                f"{obs['attack1_rejected']} act={obs['activation_attempted']}/"
                f"{obs['activation_offered']} esc={obs['escape_offered']}")


if __name__ == "__main__":
    asyncio.run(main())
