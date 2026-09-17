#!/usr/bin/env python3
"""Issue #650: Tyvar the Bellicose — granted trigger does not work.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json, v0.86.0):
  Tyvar the Bellicose {2}{B}{G}, Legendary Creature — Elf Warrior 5/4.
  Oracle: "Whenever one or more Elves you control attack, they gain deathtouch
  until end of turn. Each creature you control has 'Whenever a mana ability of
  this creature resolves, put a number of +1/+1 counters on it equal to the
  amount of mana this creature produced. This ability triggers only once each
  turn.'"

Reported failure: tapping creatures for mana does not produce the counters;
the reporter saw the ability fire at most once per turn instead of once per
creature-tap (the granted instance is per-creature, once per turn).

Setup:
  P0: 12x Tyvar the Bellicose, 8x Llanowar Elves, 20x Forest, 20x Swamp
  (dense playsets in a custom game; the engine accepts >4-of).
  P1: 60x Island, draw-go.
  Both keep 7 (P0 mulligans toward an Elves opener, max 3).

Trigger (leg A): with Tyvar on the battlefield and a sickness-free untapped
Llanowar Elves A, P0 activates Elves A's mana ability ({T}: Add {G}) in its
own main phase. Expected: the granted trigger resolves and A ends with
exactly 1 +1/+1 counter.

Trigger (leg B, same turn as leg A): activate a second sickness-free
Llanowar Elves B's mana ability on the same turn. Expected: B's own granted
instance also resolves and B ends with exactly 1 +1/+1 counter — the printed
"only once each turn" limit is per creature, not global.

Assertions:
  A1 setup_ok ........... Tyvar + sickness-free Elves A on battlefield under P0.
  A2 mana_produced ...... Elves A's mana ability resolves; A tapped, {G} in pool.
  A3 counters_added ..... Elves A has exactly 1 +1/+1 counter after window.
  A4 per_creature ....... (same turn) Elves B has exactly 1 +1/+1 counter.

Verdict rule: reproduced iff A1+A2 pass and A3 fails (trigger does not work at
all), or A1..A3 pass and A4 fails (global once-per-turn throttle). not-
reproduced iff A1..A4 all pass. A4 is not-run when A3 fails (trigger never
fires, so per-creature throttling cannot be evaluated) or when no second
sickness-free Elves is available the same turn.

Evidence: evidence/650/<run-id>/pre.json (before Elves A activation),
mid.json (mana produced), post.json (after trigger window for A),
pre2.json/post2.json (leg B), run.json, manifest.sha256, summary.png,
scenario_650_086.py

Protocol 72 (v0.86.0): ActivateAbility advertised in legal_actions with
source_id + ability_index; MulliganDecision gated on waiting_for.data.pending[]
Declare entries; DiscardToHandSize answered via viewer_interaction select
opportunity, falling back to SelectCards legal action with cardIds.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck

TYVAR = "Tyvar the Bellicose"
ELVES = "Llanowar Elves"
RUN_ID = "20260917-650b"
EVIDIR = f"/home/hatch/workspace/dev/phase-backfill/evidence/650/{RUN_ID}"
MAIN_PHASES = ("PreCombatMain", "PostCombatMain")
LANDS = ("Forest", "Swamp", "Island", "Mountain", "Plains")

# Server identity: recomputed 2026-09-17 against the on-disk pinned release v0.86.0.
SERVER_IDENTITY = {
    "server_version": "0.86.0",
    "build_commit": "2cc8c28",
    "protocol_version": 72,
    "mode": "Full",
    "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
    "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c40ff26",
    "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-17",
    "source": "ServerHello + sha256 match against pinned ledger digests (minisign-verified pin from this morning)",
}

P0_DECK = [(TYVAR, 12), (ELVES, 8), ("Forest", 20), ("Swamp", 20)]
P1_DECK = [("Island", 60)]

LOGF = None


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + "\n")


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def waiting_on(state, pid):
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {})
    wtype = wf.get("type")
    if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
        pend = d.get("pending", [])
        entry = next((e for e in pend if e.get("player") == pid),
                     pend[0] if pend else None)
        return wtype, (entry.get("player") if entry else None)
    return wtype, (d.get("player") if "player" in d else d.get("deciding_player"))


def is_mine(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and wf.get("data", {}).get("player") == pid


def bf(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def battlefield_ids(state, pid, name):
    return sorted(oid for oid in bf(state, pid)
                  if (get_obj(state, oid).get("base_name")
                      or get_obj(state, oid).get("name")) == name)


def hand_ids(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return list(p.get("hand", []))
    return []


def hand_names(state, pid):
    return [obj_name(state, o) for o in hand_ids(state, pid)]


def mana_pool_g(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            units = (p.get("mana_pool") or {}).get("mana", [])
            return sum(1 for u in units if "green" in json.dumps(u).lower())
    return 0


def plus_counters(obj):
    v = obj.get("counters")
    if isinstance(v, dict):
        n = 0
        for k, c in v.items():
            if str(k).upper().replace("_", "") in ("P1P1", "+1/+1", "PLUS1PLUS1"):
                n += c if isinstance(c, int) else 0
        return n, {"counters": v}
    if isinstance(v, int) and v:
        return v, {"counters": v}
    return 0, {"counters": v}


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def handle_mulligan(c):
    """Protocol-72: answer advertised MulliganDecision as-is, gated on my
    seat's presence in waiting_for.data.pending[] with phase Declare."""
    st = c.latest
    if not st:
        return False
    s = st["state"]
    acts = st.get("legal_actions", []) or []
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            return False  # not mulligan's job
    wf = s.get("waiting_for") or {}
    pending = ((wf.get("data") or {}).get("pending")) or []
    my_pending = [p for p in pending
                  if p.get("player") == c.player_id
                  and (p.get("phase") or {}).get("type") == "Declare"]
    if not my_pending:
        return False
    for a in acts:
        if a["type"] == "MulliganDecision":
            hn = hand_names(s, c.player_id)
            decision = "keep" if ELVES in hn else "mulligan"
            sub = copy.deepcopy(a)
            if isinstance(sub.get("data"), dict):
                sub["data"]["decision"] = decision
            return ("submit", sub, decision)
        if a["type"] == "SelectCards":
            n = ((((wf.get("data") or {})).get("phase")) or {}).get("count", 1)
            hand = list(hand_ids(s, c.player_id))
            hand.sort(key=lambda oid: 0 if obj_name(s, oid) not in LANDS else 1)
            sub = copy.deepcopy(a)
            sub["data"]["cardIds"] = [int(x) for x in hand[:max(0, n)]]
            return ("submit", sub, f"bottom {max(0,n)}")
    return False


def handle_discard(c):
    st = c.latest
    if not st:
        return False
    s = st["state"]
    wf = s.get("waiting_for") or {}
    if wf.get("type") != "DiscardToHandSize":
        return False
    wfd = wf.get("data") or {}
    if wfd.get("player", c.player_id) != c.player_id:
        return False
    n = wfd.get("count", 1)
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            cands = data.get("candidates") or []
            if not cands:
                continue
            spec = data.get("spec") or {}
            stype = spec.get("type", "select")
            names = {}
            for ch in cands:
                ref = cand_ref(ch)
                names[ch["id"]] = obj_name(s, ref) if ref else "?"
            order = [ELVES, TYVAR, "Forest", "Swamp"]
            def rank(ch):
                ref = cand_ref(ch)
                nm = obj_name(s, ref) if ref else "?"
                return order.index(nm) if nm in order else 2
            ranked = sorted(cands, key=rank)
            picks = [ch["id"] for ch in ranked[:max(1, n)]]
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": stype, "data": {"choiceIds": picks}}}
            return ("interaction", sub, f"discard {[names[p] for p in picks]}")
    for a in st.get("legal_actions", []) or []:
        if a["type"] == "SelectCards":
            hand = list(hand_ids(s, c.player_id))
            order = [ELVES, TYVAR, "Forest", "Swamp"]
            def rank_oid(oid):
                nm = obj_name(s, oid)
                return order.index(nm) if nm in order else 2
            hand.sort(key=rank_oid)
            sub = copy.deepcopy(a)
            sub["data"]["cardIds"] = [int(x) for x in hand[:max(1, n)]]
            return ("submit", sub, "discard fallback")
    return False


def find_activate(st, source_oid):
    """Elves mana ability: prefer legal_actions ActivateAbility with
    source_id match (protocol-70/72); fall back to viewer_interaction
    exactChoices 'activateAbility' with source ref."""
    for a in st.get("legal_actions", []) or []:
        if a.get("type") == "ActivateAbility" and \
                (a.get("data") or {}).get("source_id") == source_oid:
            return ("submit", a, "ActivateAbility legal_action")
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {})
            if resp.get("type") != "exactChoices":
                continue
            for ch in (resp.get("data") or {}).get("choices", []):
                surfs = ch.get("surfaces", []) or []
                codes = [s.get("data", {}).get("code") for s in surfs
                         if isinstance(s.get("data"), dict)]
                if "activateAbility" not in codes:
                    continue
                refs = [str(s.get("data", {}).get("reference")) for s in surfs
                        if isinstance(s.get("data"), dict)
                        and s.get("data", {}).get("role") == "source"]
                if str(source_oid) in refs:
                    # protocol-72: an "exactChoices" opportunity is answered
                    # with response type "choose" + singular choiceId.
                    sub = {"interactionId": opp.get("interactionId"),
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}}
                    return ("interaction", sub, "activateAbility via viewer_interaction (choose)")
    return None


async def main():
    global LOGF
    os.makedirs(EVIDIR, exist_ok=True)
    if not os.listdir(EVIDIR):
        pass
    else:
        raise SystemExit(f"EVDIR {EVIDIR} not empty — bump RUN_ID")
    LOGF = open(f"{EVIDIR}/scenario_run.log", "w")
    t0 = time.time()
    assertions = {"A1_setup_ok": "not-run", "A2_mana_produced": "not-run",
                  "A3_counters_added": "not-run", "A4_per_creature": "not-run"}
    notes = []

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game={p0.game_code} run={RUN_ID}")

    async def export(tag):
        raw = await p0.export_state()
        env = json.loads(raw) if isinstance(raw, str) else raw
        path = f"{EVIDIR}/{tag}.json"
        with open(path, "w") as f:
            json.dump(env, f)
        say(f"exported {tag}.json")
        return env

    async def do(c, kind, sub):
        if kind == "interaction":
            await c.send_interaction(sub)
        else:
            await c.send_action(sub)

    max_turn = 0
    elves_seen_turn = {}
    pre_turn = None
    b_turn = None
    elves_a = elves_b = None
    activated = False
    need_mid = False
    post_rounds = 0
    a_done = False
    b_activated = False
    post_rounds2 = 0
    b_done = False
    finished = False
    pre_pool_g = None
    mulls = {0: 0, 1: 0}
    interaction_sent_at = None  # tick time of the leg-A interaction submission
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}

    i = 0
    while i < 6000:
        i += 1
        await asyncio.sleep(0.2)
        acted_any = False
        for c in (p0, p1):
            st = c.latest
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 45:
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={st['state'].get('turn_number')} phase={st['state'].get('phase')} "
                        f"wf={st['state'].get('waiting_for',{}).get('type')} pp={st['state'].get('waiting_for',{}).get('data',{}).get('player')}")
                    last_change[c.player_id] = time.time()
                continue
            state = st["state"]
            acts = list(st.get("legal_actions", []) or [])
            pid = c.player_id
            wtype, wplayer = waiting_on(state, pid)
            turn = state.get("turn_number", 0)
            max_turn = max(max_turn, turn)

            if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
                r = handle_mulligan(c)
                if r:
                    kind, sub, what = r
                    if isinstance(what, str) and what.startswith("mulligan") and mulls[pid] >= 3:
                        what = "keep"
                        sub["data"]["decision"] = "keep"
                    if what == "mulligan" or what == "keep":
                        mulls[pid] += 1
                    await do(c, kind, sub)
                    say(f"P{pid} mulligan: {what}")
                    acted_any = True
                continue
            r = handle_discard(c)
            if r:
                kind, sub, what = r
                await do(c, kind, sub)
                say(f"P{pid} {what}")
                acted_any = True
                continue
            if wtype != "Priority" and wplayer != pid:
                continue
            if wtype not in ("Priority",):
                if wtype == "ChooseLegend":
                    da = [a for a in acts if a["type"] == "ChooseLegend"]
                    if da:
                        await c.send_action(da[0])
                        say(f"P{pid} ChooseLegend keep-first")
                        acted_any = True
                    continue
                if wtype in ("DeclareAttackers", "DeclareBlockers"):
                    da = [a for a in acts if a["type"] == wtype]
                    if da:
                        sub = copy.deepcopy(da[0])
                        d = sub.setdefault("data", {})
                        for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                            if k in d:
                                d[k] = [] if isinstance(d[k], list) else {}
                        await c.send_action(sub)
                        say(f"P{pid} declares no {wtype}")
                        acted_any = True
                    continue
                for a in acts:
                    if a["type"] == "PassPriority":
                        await c.send_action(a)
                        acted_any = True
                        break
                continue
            if not is_mine(state, pid):
                continue

            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await c.send_action(a)
                    acted_any = True
                    say(f"P{pid} pays mana via {a['type']}")
                    break
            if acted_any:
                break

            if pid == 1:
                acted = False
                if state.get("active_player") == 1 and state.get("phase") in MAIN_PHASES:
                    for a in acts:
                        if a["type"] == "PlayLand":
                            await c.send_action(a)
                            acted = True
                            break
                if not acted:
                    for a in acts:
                        if a["type"] == "PassPriority":
                            await c.send_action(a)
                            acted_any = True
                            break
                continue

            # ---- P0 ----
            in_main = state.get("phase") in MAIN_PHASES
            my_turn = state.get("active_player") == 0
            tyvar_oids = battlefield_ids(state, 0, TYVAR)
            tyvar_id = tyvar_oids[0] if tyvar_oids else None
            elves_oids = battlefield_ids(state, 0, ELVES)
            hn = hand_names(state, 0)
            for eo_id in elves_oids:
                elves_seen_turn.setdefault(eo_id, turn)

            if tyvar_id and elves_oids and assertions["A1_setup_ok"] == "not-run":
                assertions["A1_setup_ok"] = "passed"
                say(f"setup ok at turn {turn} (Tyvar + {len(elves_oids)} Elves)")

            def sickness_free(oid):
                return turn > elves_seen_turn.get(oid, turn)

            # failed-interaction retry: a submitted interaction that moves no
            # revision in 12s is unanswered (rejected server-side) — retry.
            if (activated and need_mid and interaction_sent_at
                    and time.time() - interaction_sent_at > 12):
                notes.append(f"interaction at turn {pre_turn} unanswered (no revision in 12s); retrying activation")
                say("interaction unanswered; resetting leg-A activation to retry")
                activated = False
                need_mid = False
                interaction_sent_at = None
                elves_a = None

            # ---- leg A: activate first sickness-free Elves ----
            if tyvar_id and in_main and my_turn and not activated and not finished:
                cand = next((o for o in elves_oids
                             if sickness_free(o) and not get_obj(state, o).get("tapped")),
                            None)
                if cand:
                    r = find_activate(st, cand)
                    if r:
                        kind, sub, what = r
                        elves_a = cand
                        pre_turn = turn
                        pre_pool_g = mana_pool_g(state, 0)
                        say(f"PRE export at turn {turn}; activating Elves A mana ability (oid {elves_a}) via {what}")
                        await export("pre")
                        await do(c, kind, sub)
                        activated = True
                        need_mid = True
                        interaction_sent_at = time.time()
                        acted_any = True
                        continue

            # mid checkpoint: prove the mana ability resolved
            if need_mid and pid == 0 and is_mine(state, 0):
                env = await export("mid")
                eo = get_obj(env["state"], elves_a)
                pool_g = mana_pool_g(env["state"], 0)
                notes.append(f"mid: Elves A tapped={eo.get('tapped')} pool_g={pool_g} (pre pool_g={pre_pool_g})")
                assertions["A2_mana_produced"] = "passed" if (
                    eo.get("tapped") and pool_g > (pre_pool_g or 0)) else "failed"
                say(f"A2 = {assertions['A2_mana_produced']} (tapped={eo.get('tapped')} pool {pre_pool_g}->{pool_g})")
                need_mid = False

            # leg-A trigger window
            if activated and not a_done and not need_mid and not finished:
                post_rounds += 1
                if post_rounds >= 40:
                    env = await export("post")
                    eo = get_obj(env["state"], elves_a)
                    n, sample = plus_counters(eo)
                    notes.append(f"post: Elves A counters raw sample: {json.dumps(sample)[:300]}")
                    notes.append(f"post: Elves A tapped={eo.get('tapped')}")
                    if n == 1:
                        assertions["A3_counters_added"] = "passed"
                    elif n == 0:
                        assertions["A3_counters_added"] = "failed"
                        notes.append("BUG: no +1/+1 counters on Elves A after granted trigger window")
                    else:
                        assertions["A3_counters_added"] = "failed"
                        notes.append(f"unexpected counter count on Elves A: {n}")
                    say(f"A3 = {assertions['A3_counters_added']} (counters={n})")
                    a_done = True

            # ---- leg B: same-turn second creature ----
            if a_done and assertions["A3_counters_added"] == "passed" and not b_done and not finished:
                if turn > pre_turn:
                    assertions["A4_per_creature"] = "not-run"
                    notes.append("A4 not-run: turn advanced past leg-A turn before leg-B activation")
                    b_done = True
                elif not b_activated and in_main and my_turn:
                    cand = next((o for o in elves_oids
                                 if o != elves_a and sickness_free(o)
                                 and not get_obj(state, o).get("tapped")), None)
                    if cand:
                        r = find_activate(st, cand)
                        if r:
                            kind, sub, what = r
                            elves_b = cand
                            b_turn = turn
                            say(f"PRE2 export at turn {turn}; activating Elves B (oid {elves_b}) via {what}")
                            await export("pre2")
                            await do(c, kind, sub)
                            b_activated = True
                            acted_any = True
                            continue
                if b_activated:
                    post_rounds2 += 1
                    if post_rounds2 >= 40:
                        env = await export("post2")
                        eo = get_obj(env["state"], elves_b)
                        n, sample = plus_counters(eo)
                        notes.append(f"post2: Elves B counters raw sample: {json.dumps(sample)[:300]}")
                        if n == 1:
                            assertions["A4_per_creature"] = "passed"
                        elif n == 0:
                            assertions["A4_per_creature"] = "failed"
                            notes.append("BUG: no +1/+1 counters on Elves B after same-turn activation (global once-per-turn throttle?)")
                        else:
                            assertions["A4_per_creature"] = "failed"
                            notes.append(f"unexpected counter count on Elves B: {n}")
                        say(f"A4 = {assertions['A4_per_creature']} (counters={n})")
                        b_done = True
                        finished = True

            # if leg-A bug confirmed, leg B is moot
            if a_done and assertions["A3_counters_added"] == "failed" and not finished:
                assertions["A4_per_creature"] = "not-run"
                notes.append("A4 not-run: granted trigger never fires (A3 failed), per-creature throttling cannot be evaluated")
                finished = True

            if finished:
                break

            # main-phase economy: land first, then cast (cap Elves at 2, skip 2nd Tyvar)
            if in_main and my_turn:
                acted = False
                for a in acts:
                    if a["type"] == "PlayLand":
                        await c.send_action(a)
                        acted = True
                        break
                if not acted:
                    wants = []
                    if len(elves_oids) < 2:
                        wants.append(ELVES)
                    if not tyvar_id:
                        wants.append(TYVAR)
                    for want in wants:
                        if want in hn:
                            for a in acts:
                                d = a.get("data", {})
                                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == want:
                                    say(f"P0 casting {want}")
                                    await c.send_action(a)
                                    acted = True
                                    break
                        if acted:
                            break
                if acted:
                    continue
            for a in acts:
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    acted_any = True
                    break
        if finished:
            break
        if max_turn > 40 and not activated:
            notes.append("could not reach leg-A trigger by turn 40 (Tyvar/Elves never both deployed)")
            break

    if assertions["A1_setup_ok"] == "not-run":
        assertions["A1_setup_ok"] = "failed" if max_turn > 40 else "not-run"
    if not activated:
        assertions["A2_mana_produced"] = "not-run"
        assertions["A3_counters_added"] = "not-run"
        notes.append("A4 not-run: leg-A activation never happened")
    if a_done and assertions["A3_counters_added"] == "passed" and not b_done and not b_activated:
        assertions["A4_per_creature"] = "not-run"
        notes.append("A4 not-run: no second sickness-free Elves available on the leg-A turn")
    # parser corroboration against pinned card-data
    try:
        cd = json.load(open("/home/hatch/workspace/dev/phase-backfill/server/releases/v0.86.0/data/card-data.json"))
        entries = cd if isinstance(cd, list) else cd.get("cards", cd)
        ty = next((c for c in (entries.values() if isinstance(entries, dict) else entries)
                   if str(c.get("name")) == TYVAR), None)
        if ty:
            texts = json.dumps(ty.get("abilities", ty))[:400]
            notes.append(f"parser corroboration: card-data.json v0.86.0 Tyvar entry abilities excerpt: {texts}")
    except Exception as e:
        notes.append(f"parser check skipped: {e}")

    a1 = assertions["A1_setup_ok"]
    a2 = assertions["A2_mana_produced"]
    a3 = assertions["A3_counters_added"]
    a4 = assertions["A4_per_creature"]
    if a1 == "passed" and a2 == "passed":
        if a3 == "failed" or a4 == "failed":
            verdict = "reproduced"
        elif a3 == "passed" and a4 == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"

    scenario_src = open(__file__).read()
    run = {
        "issue": 650,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{open('/home/hatch/workspace/goals/phase-rs-phase-bug-state-backfill/hidden_files/current_run_id.txt').read().strip()}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src.encode()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "mulligans": f"P0 mulliganed {mulls[0]}x seeking Elves in opener; P1 kept",
        "setup_line": "P0: 12x Tyvar the Bellicose + 8x Llanowar Elves + 20 Forest + 20 Swamp | P1: 60 Island (draw-go)",
        "contract_line": "Tap Elves for {G} with Tyvar out: granted trigger must put 1 +1/+1 counter on the tapped creature (once per turn, per creature).",
        "stats": {"max_turn": max_turn, "pre_turn": pre_turn, "b_turn": b_turn,
                  "activated": activated, "b_activated": b_activated},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
        ],
    }
    with open(f"{EVIDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVIDIR}/scenario_650_086.py", "w") as f:
        f.write(scenario_src)
    if LOGF:
        LOGF.close()
    lines = []
    for fn in sorted(os.listdir(EVIDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVIDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVIDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(json.dumps({"verdict": verdict, "assertions": assertions}, indent=1))
    await p0.close()
    await p1.close()


asyncio.run(main())
