#!/usr/bin/env python3
"""Issue #6874: game stuck after attacking with Emrakul, the Aeons Torn.

Re-validation (2026-10-01) of the 2026-09-11 v0.80.0/protocol-69 run
(verdict not-reproduced) on the pinned v0.99.0 (build d919616, protocol 98).
The maintained evidence comment already publishes the v0.80.0 result; per
the playbook staleness rule this run re-exercises the reported path on the
current pin.

Protocol-98 driver notes (v0.99.0, 2026-10-01):
- HELLO advertises protocol 98 (server enforces exact match).
- MulliganDecision as {"choice":{"type":"Keep"}}, gated on
  waiting_for.data.pending[] Declare entries keyed by (client, revision).
- BottomCards / DiscardToHandSize via single SelectCards {"cards":[...]}.
- PassPriority only when the seat genuinely holds priority (waiting_for
  Priority names the player), revision-guarded.


Reported: after the reporter declared Emrakul as an attacker, the game
stuck. Console showed:

    [Debug] AI controller halted after 3 failed proposals on EffectZoneChoice

Triage (mike-theDude, 2026-08-03): the annihilator-6 attack trigger builds
a six-permanent sacrifice choice for the defending player; the AI's
proposal round trip for that supported choice is the suspect.

A follow-up (2026-08-11) notes #7241 fixes only minimum-blocker AI
blocking, NOT the annihilator sacrifice EffectZoneChoice softlock, which
is where the reporter's diagnostic points. The saved game state attached
to the issue is on a time-limited Discord CDN link (likely expired).

Plan (native engine, v0.80.0 / protocol 69, native Medium AI defender):
  GAME A (reported branch): P0 (human driver) casts Emrakul, declares it
          as an attacker at P1 (native AI). Annihilator 6 triggers; the AI
          must sacrifice six permanents (EffectZoneChoice for the AI).
          Expected per acceptance criteria: the AI proposes a legal
          complete selection; the game proceeds past the annihilator
          choice without exhausting proposal retries; exactly six
          permanents are sacrificed.
  GAME B (control, same game): continue playing past the annihilator
          resolution to confirm the game proceeds normally.

Behavioral contract:
  A1 setup_ok         P0 DeclareAttackers with Emrakul on the battlefield
                      and attack-ready (extra turn from the cast trigger);
                      P1 (AI) has >= 6 permanents
  A2 annihilator      annihilator trigger entered the stack after the
                      attack declaration
  A3 ai_sacrifice     defending AI sacrificed EXACTLY 6 permanents in
                      response to the trigger (P1 battlefield count -6),
                      zero failed AI proposals in the server log
  A4 no_softlock      game advanced past the annihilator resolution (no
                      120s stall, no "AI controller halted" line in the
                      server log), combat completed
  A5 cleanup          stack empty, game proceeding after combat

Verdict: reproduced iff A1 passes and (A3 fails: AI does not sacrifice
exactly 6 / proposals fail) or A4 fails (stall or AI halt observed).
not-reproduced iff A1..A5 all pass.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import hashlib  # noqa: E402


BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6874"

SERVER_IDENTITY = {
    "validated_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}

def _sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

# recompute against on-disk artifacts; never copy hashes blindly
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)
EVDIR = f"{BACKFILL}/evidence/6874/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

EMRAKUL = "Emrakul, the Aeons Torn"
BEAR = "Grizzly Bears"
FOREST = "Forest"
P0_DECK = [(EMRAKUL, 8), (BEAR, 8), (FOREST, 44)]
P1_AI_DECK = [(BEAR, 4), (FOREST, 56)]
LANDS = (FOREST,)
TIMEOUT = 2400
STALL_AFTER = 120  # seconds on one AI decision before declaring a stall

ST = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
LAST_SUBMIT = {"iid": None}
C0 = None


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "cast_submitted_at": None, "emrakul_enter_turn": None,
        "attack_declared_at": None, "annihilator_seen": False,
        "attack_submitted_at": None, "attack_emrakul_oid": None,
        "attack_rejected": False, "mid_blockers_exported": False,
        "annihilator_resolved_at": None,
        "pre_exported": False, "mid_exported": False,
        "post_exported": False, "final_exported": False,
        "p1_count_mid": None, "p1_count_post": None,
        "p1_gy_mid": None, "mid_trigger_verified": False,
        "p1_count_blockers": None, "p1_gy_blockers": None,
        "stall_at": None, "stall_kind": None,
        "ai_wait_start": None, "ai_wait_wf": None,
        "stop": False, "retry": False,
        "rejections": [],
        "attack_turn": None,
    })
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    LAST_SUBMIT.update({"iid": None})


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def emrakul_bf(state, pid):
    return [oid for oid, o in bf(state, pid) if oname(o) == EMRAKUL]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def pending_for(state, pid):
    for p in wf_data(state).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    return (wf_type(state) == "Priority"
            and str(wf_player(state)) == str(pid))


_PASSED_REV = {}
_DISCARD_REV = {}


async def do_mulligan(c, pid):
    """Protocol-98 MulliganDecision: {"choice":{"type":"Keep"}}, gated on the
    seat's pending[] Declare phase; revision-guarded."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    if MULLS.get((c.name, c.revision)):
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    n_lands = sum(1 for o in hand_oids(state, pid)
                  if oname(state["objects"][o]) in LANDS)
    has_em = find_hand(state, pid, EMRAKUL) is not None
    keep_ok = (has_em and n_lands >= 2) or MULLS.get(c.name, 0) >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        MULLS[c.name] = MULLS.get(c.name, 0) + 1
    MULLS[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice}")
    return True


async def do_bottom(c, pid):
    """BottomCards after Declare: single SelectCards {"cards":[...]}."""
    st = c.latest
    if not st:
        return False
    if MULLS.get((c.name, "bottom", c.revision)):
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    ph = pend.get("phase") or {}
    if ph.get("type") != "BottomCards":
        return False
    n = int(ph.get("count", 1) or 1)
    picks = [int(x) for x in hand_oids(state, pid)[:n]]
    MULLS[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid):
    """Protocol-98 DiscardToHandSize: single SelectCards {"cards":[...]}.
    Keep one Emrakul + lands; discard extra Emrakuls first."""
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_type(state) != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_oids(state, pid)) - 7
    if n <= 0:
        return False
    h = hand_oids(state, pid)
    keep_oid = find_hand(state, pid, EMRAKUL)
    extras = [o for o in h if o != keep_oid
              and oname(state["objects"][o]) == EMRAKUL]
    pref = extras
    pref += [o for o in h if o not in pref and o != keep_oid
             and oname(state["objects"][o]) not in LANDS]
    pref += [o for o in h if o not in pref and o != keep_oid]
    picks = [int(x) for x in pref[:n]]
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    await submit_as_is(c, {"type": "SelectCards",
                           "data": {"cards": picks}})
    say(f"{c.name} discards {len(picks)}")
    return True


async def pass_priority(c, pid):
    """Pass only when this seat genuinely holds priority (waiting_for
    Priority names them); revision-guarded against double passes."""
    st = c.latest
    if not st:
        return False
    if not my_priority(st["state"], pid):
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get((c.name, rev)):
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            _PASSED_REV[(c.name, rev)] = True
            await submit_as_is(c, a)
            return True
    return False


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    """Export authoritative state to EVDIR/path. Never raises: on
    failure logs and returns None so the main loop survives and can
    retry."""
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def stack_has_annihilator(state):
    """Strict annihilator-attack-trigger detection: kind.type ==
    TriggeredAbility with 'annihilator' in the trigger's own ability
    description (per the #6773 lesson: never bare-substring the whole
    entry, the source card's own text false-positives)."""
    for e in (state.get("stack") or []):
        if not isinstance(e, dict):
            continue
        kind = e.get("kind") or {}
        if not isinstance(kind, dict) or kind.get("type") != "TriggeredAbility":
            continue
        ab = e.get("ability") or {}
        desc = str((ab.get("description") if isinstance(ab, dict) else "")
                   or "")
        if "annihilator" in desc.lower():
            return True
    return False


def emrakul_attacking(state, oid):
    """True iff Emrakul (oid) is recorded as an attacker in
    state.combat.attackers. Matches the oid as a JSON number, never a
    substring (oid 4 must not match 14/40)."""
    attackers = (state.get("combat") or {}).get("attackers") or []
    for a in attackers:
        try:
            blob = json.dumps(a, default=str)
        except Exception:
            continue
        if (f":{oid}," in blob or f":{oid}}}" in blob
                or f"[{oid}," in blob or f"[{oid}]" in blob
                or blob.strip() == str(oid)):
            return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{c.name} legend-choice submitted as-is: {a['type']}")
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            # block each attacker with an untapped bear; attackers tap
            # when declared
            sub = copy.deepcopy(a)
            attackers = [oid for oid, o in bf(state, 1)
                         if o.get("tapped") and "Creature" in str(
                             o.get("type_line") or "")]
            if not attackers:
                # fallback: any tapped opponent creature
                attackers = [oid for oid, o in bf(state, 1)
                             if o.get("tapped")
                             and oname(o) == BEAR]
            blockers = [oid for oid, o in bf(state, pid)
                        if oname(o) == BEAR and not o.get("tapped")]
            sub["data"]["assignments"] = [
                [int(blockers[i]), int(attackers[i])]
                for i in range(min(len(blockers), len(attackers)))]
            if sub["data"]["assignments"]:
                say(f"[P0] blocks {len(sub['data']['assignments'])} attackers")
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # P0 attack logic: declare Emrakul on a turn after it entered;
    # submit an (empty) declaration on other DeclareAttackers so the
    # game never waits on us
    if state.get("active_player") == 0 \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            em_oids = emrakul_bf(state, 0)
            can_attack = (
                em_oids
                and ST["stage"] in ("ATTACK_ARMED", "ATTACK_WINDOW",
                                   "RESOLVED")
                and ST.get("emrakul_enter_turn") is not None
                and (state.get("turn_number") or 0)
                > ST["emrakul_enter_turn"])
            sub = copy.deepcopy(a)
            if can_attack and ST["attack_declared_at"] is None \
                    and ST.get("attack_submitted_at") is None:
                if not ST["pre_exported"]:
                    if await export_now("pre_attack.json") is not None:
                        ST["pre_exported"] = True
                    wire("pre_attack",
                         {"p1_permanents": len(bf(state, 1)),
                          "p0_permanents": len(bf(state, 0)),
                          "turn": state.get("turn_number")})
                # wire expects u64 attacker ids (string oids are rejected
                # as invalid messages)
                sub["data"]["attacks"] = [
                    [int(em_oids[0]), {"type": "Player", "data": 1}]]
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                ST["attack_submitted_at"] = time.time()
                ST["attack_turn"] = state.get("turn_number")
                ST["attack_emrakul_oid"] = int(em_oids[0])
                say(f"[P0] submitted Emrakul attack "
                    f"(oid={em_oids[0]}) turn={state.get('turn_number')}")
            else:
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return True
    if is_my_main(state, pid) and ST["stage"] == "SETUP":
        if await p0_land_drop(c, pid, state, acts):
            return True
        if await p0_maybe_cast(c, pid, state, acts):
            return True
        if await p0_maybe_cast_bear(c, pid, state, acts):
            return True
    if await pass_priority(c, pid):
        return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, FOREST)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def p0_maybe_cast_bear(c, pid, state, acts):
    # build blockers while ramping; 2 mana, keep 15-mana plan intact by
    # only spending when we have mana to spare
    if untapped_of(state, pid, FOREST) < 2:
        return False
    oid = find_hand(state, pid, BEAR)
    if not oid:
        return False
    if len([o for o in bf(state, pid) if oname(o[1]) == BEAR]) >= 8:
        return False
    a = castspell_advertised(acts, oid)
    if not a:
        return False
    await submit_as_is(c, a)
    say(f"[P0] casts {BEAR} (oid={oid})")
    return True


async def p0_maybe_cast(c, pid, state, acts):
    if untapped_of(state, pid, FOREST) < 15:
        return False
    if not find_hand(state, pid, EMRAKUL):
        return False
    if ST.get("cast_submitted_at"):
        return False
    oid = find_hand(state, pid, EMRAKUL)
    a = castspell_advertised(acts, oid)
    if not a:
        return False
    await submit_as_is(c, a)
    ST["cast_submitted_at"] = time.time()
    ST["stage"] = "CAST_WINDOW"
    say(f"[P0] casts Emrakul (oid={oid})")
    return True


def ai_stall_signatures(log_path):
    """Scan the server log for the reported AI-halt diagnostic and the
    native AI loop-stop signature."""
    hits = []
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                low = line.lower()
                if "halted after" in low and "failed proposals" in low:
                    hits.append(("ai_halted", line.strip()[:300]))
                if "choose_action returned none" in low \
                        and "stopping ai loop" in low:
                    hits.append(("ai_loop_stop", line.strip()[:300]))
                if "effectzonechoice" in low and (
                        "fail" in low or "reject" in low or "halt" in low):
                    hits.append(("effectzonechoice_event",
                                 line.strip()[:300]))
    except FileNotFoundError:
        pass
    return hits


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList",
                                        "data": ai_deck}}])
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = native AI Medium")
    wire("game_created", {"code": p0.game_code,
                          "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})

    last_rev = -1
    last_tick_wall = 0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        rej = drain_rejections(p0)
        if rej:
            ST["rejections"].extend(
                {"at": now, "who": p0.name, "type": r["type"],
                 "data": r["data"]} for r in rej)
            if ST.get("attack_submitted_at") \
                    and not ST.get("attack_declared_at"):
                ST["attack_rejected"] = True
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            last_rev = p0.revision
        elif p0.revision != last_rev:
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            last_rev = p0.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        # Emrakul resolved on the battlefield -> arm the attack
        if ST["stage"] == "CAST_WINDOW" and emrakul_bf(state, 0):
            ST["emrakul_enter_turn"] = state.get("turn_number")
            ST["stage"] = "ATTACK_ARMED"
            say(f"Emrakul on BF turn {ST['emrakul_enter_turn']} "
                f"(extra turn granted: {bool(state.get('extra_turns'))})")

        # attack submitted -> confirm it landed. Two independent
        # signals: Emrakul recorded in combat.attackers, or the game
        # advancing to DeclareBlockers with no rejection. A rejection
        # after submission clears the submission so the driver retries.
        if ST["stage"] == "ATTACK_ARMED" and ST.get("attack_submitted_at") \
                and not ST.get("attack_declared_at"):
            if ST.get("attack_rejected"):
                ST["attack_submitted_at"] = None
                ST["attack_rejected"] = False
                say("attack submission rejected; will retry next "
                    "DeclareAttackers")
            elif emrakul_attacking(state, ST.get("attack_emrakul_oid")) \
                    or (state.get("phase") == "DeclareBlockers"):
                ST["attack_declared_at"] = now
                ST["stage"] = "ATTACK_WINDOW"
                say(f"attack confirmed: Emrakul "
                    f"(oid={ST['attack_emrakul_oid']}) attacking P1, "
                    f"turn={state.get('turn_number')} "
                    f"phase={state.get('phase')}")

        # DeclareBlockers checkpoint: the annihilator trigger (if any)
        # has resolved by now; the AI cannot play lands mid-combat, so
        # the P1 battlefield/graveyard delta vs pre_attack isolates the
        # sacrifice outcome.
        if ST["stage"] == "ATTACK_WINDOW" \
                and not ST.get("mid_blockers_exported") \
                and state.get("phase") == "DeclareBlockers":
            s = await export_now("mid_blockers.json")
            if s is None:
                # export failed; retry on a later DeclareBlockers tick
                # (do not mark exported)
                pass
            else:
                ST["mid_blockers_exported"] = True
                try:
                    env = json.loads(s)
                    bs = env["state"]
                    bobjs = bs["objects"]
                    p1b = sum(1 for o in bobjs.values()
                              if o.get("zone") == "Battlefield"
                              and o.get("controller") == 1)
                    p1g = len(bs["players"][1].get("graveyard", []))
                except Exception:
                    p1b, p1g = None, None
                ST["p1_count_blockers"] = p1b
                ST["p1_gy_blockers"] = p1g
                wire("mid_blockers", {"p1_permanents": p1b, "p1_gy": p1g,
                                      "turn": state.get("turn_number")})
                say(f"DeclareBlockers checkpoint: P1 permanents={p1b} "
                    f"gy={p1g}")

        # annihilator trigger on the stack -> mid export, start AI watch
        if ST["stage"] == "ATTACK_WINDOW" and not ST["annihilator_seen"] \
                and stack_has_annihilator(state):
            ST["annihilator_seen"] = True
            ST["p1_count_mid"] = len(bf(state, 1))
            ST["p1_gy_mid"] = len(state["players"][1].get("graveyard", []))
            if not ST["mid_exported"]:
                s = await export_now("mid_annihilator.json")
                if s is None:
                    s = "{}"
                else:
                    ST["mid_exported"] = True
                # verify the exported bytes actually caught the trigger
                # (the cached state can race the export)
                try:
                    env = json.loads(s)
                    mid_ok = stack_has_annihilator(env["state"])
                except Exception:
                    mid_ok = False
                ST["mid_trigger_verified"] = mid_ok
                wire("mid_annihilator",
                     {"p1_permanents": ST["p1_count_mid"],
                      "p1_gy": ST["p1_gy_mid"],
                      "waiting_for": state.get("waiting_for"),
                      "turn": state.get("turn_number"),
                      "phase": state.get("phase"),
                      "trigger_in_export": mid_ok})
            say(f"annihilator trigger on stack; P1 permanents={ST['p1_count_mid']}")

        # stall watchdog for the attack window: the reported bug is a
        # hard stop with the AI's EffectZoneChoice unanswered. Track
        # turn/phase progress; 120s without progress after the attack
        # is confirmed = stall. (The native AI answers synchronously,
        # so the trigger itself may never be visible to this client.)
        if ST["stage"] == "ATTACK_WINDOW" and not ST.get("stall_at"):
            prog = (state.get("turn_number"), state.get("phase"),
                    wf_type(state), wf_player(state))
            if prog != ST.get("last_progress"):
                ST["last_progress"] = prog
                ST["progress_at"] = now
            elif now - ST.get("progress_at", now) > STALL_AFTER:
                ST["stall_at"] = now
                ST["stall_kind"] = (f"no_progress_120s_turn{prog[0]}_"
                                    f"{prog[1]}_wf{prog[2]}_p{prog[3]}")
                say(f"STALL ({ST['stall_kind']})")
                wire("stall", {"kind": ST["stall_kind"],
                               "wf": state.get("waiting_for")})
                await export_now("mid_stall.json")
                ST["stop"] = True  # export_now never raises
        # post-resolution: keep playing a little to show the game
        # proceeds, then export the final state and stop. Keyed off the
        # blockers checkpoint (the trigger itself may resolve faster
        # than the client's poll when the native AI answers
        # synchronously).
        if ST.get("mid_blockers_exported") \
                and not ST.get("final_exported"):
            if ST.get("post_timer_start") is None:
                ST["post_timer_start"] = now
            if now - ST["post_timer_start"] > 90:
                if await export_now("post.json") is not None:
                    ST["final_exported"] = True
                    ST["stop"] = True
                else:
                    # retry the final export shortly
                    ST["post_timer_start"] = now - 75

    await p0.close()
    log_path = f"{BACKFILL}/runs/{RUN_ID}/server.log"
    hits = ai_stall_signatures(log_path)
    for kind, line in hits:
        say(f"SERVERLOG [{kind}]: {line}")
        wire("serverlog_hit", {"kind": kind, "line": line})
    return dict(ST), hits


if __name__ == "__main__":
    st, hits = asyncio.run(main())
    print(json.dumps({
        "stage": st.get("stage"),
        "cast_submitted_at": bool(st.get("cast_submitted_at")),
        "attack_declared_at": bool(st.get("attack_declared_at")),
        "annihilator_seen": st.get("annihilator_seen"),
        "mid_trigger_verified": st.get("mid_trigger_verified"),
        "p1_count_blockers": st.get("p1_count_blockers"),
        "p1_gy_blockers": st.get("p1_gy_blockers"),
        "p1_count_mid": st.get("p1_count_mid"),
        "p1_count_post": st.get("p1_count_post"),
        "annihilator_resolved_at": bool(st.get("annihilator_resolved_at")),
        "mid_blockers_exported": bool(st.get("mid_blockers_exported")),
        "final_exported": bool(st.get("final_exported")),
        "stall_kind": st.get("stall_kind"),
        "rejections": len(st.get("rejections", [])),
        "serverlog_hits": [h[0] for h in hits],
    }, indent=2, default=str))
