#!/usr/bin/env python3
"""Issue #7347: Day of Black Sun cast for X=5 destroyed nothing.

Oracle (pinned v0.99.0 card data):
  Day of Black Sun {X}{B}{B} Sorcery: "Each creature with mana value X or
  less loses all abilities until end of turn. Destroy those creatures."
Parsed: Continuous head (remove all abilities, affects Typed Creature with
Cmc LE Ref(X)) -> Destroy {target: TrackedSet 0}. Triage: the continuous
head publishes an empty tracked set, so the Destroy destroys nothing. A
second Discord report (X=0) confirms the destroy half never executes.

Run: 20261001-7347 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  Leg 1 (reported case): P0 casts Day of Black Sun for X=5 with creatures of
  MV<=5 on both battlefields (P0: Memnites MV 0; P1: Memnites MV 0 + Serra
  Angels MV 5 with flying+vigilance). Expect every MV<=5 creature destroyed
  (in a graveyard) after resolution.
  A3 (partial-effect check): in the reproduced case the Angels stay on the
  battlefield; their keywords must be gone (lose-all-abilities applied).

  A1 setup_ok        pre.json: P0 main, P0 priority, >=7 untapped Swamps,
                    Day of Black Sun in P0 hand, >=2 Memnites on P0 BF,
                    >=1 Serra Angel + >=1 Memnite on P1 BF, life 20/20,
                    stack empty
  A2 cast_resolved   x_prompt_seen offered an X choice; driver answered 5;
                    post.json: Day of Black Sun in P0 graveyard, stack empty
  A3 abilities_gone  (conditional) Angels still on P1 BF in post -> their
                    keywords are empty. If all Angels were destroyed, not-run.
  A4 creatures_gone  THE REPORTED OUTCOME: every creature on the BF in pre
                    with MV<=5 is in a graveyard in post.
  A5 cleanup         post stack empty; game proceeding; rejections noted.

Verdict: reproduced iff A1+A2 pass and A4 fails.
not-reproduced iff A1+A2+A4 pass. blocked iff A1 or A2 fails.

Card-name note: this engine's state reports Title-Case card names
("Day of Black Sun", "Memnite", "Swamp", "Serra Angel", "Plains").
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
RUN_ID = "20261001-7347"
ISSUE = 7347

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


for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DOS = "Day of Black Sun"
MEMNITE = "Memnite"
SWAMP = "Swamp"
ANGEL = "Serra Angel"
PLAINS = "Plains"
# Dense playsets are a test-harness convenience (the engine accepts
# >4-of for custom games); they make the setup draw-reliable.
P0_DECK = [(DOS, 8), (MEMNITE, 12), (SWAMP, 40)]
P1_DECK = [(ANGEL, 8), (MEMNITE, 16), (PLAINS, 36)]
P0_LANDS = (SWAMP,)
P1_LANDS = (PLAINS,)
X_VALUE = 5
TIMEOUT = 1500

ST = {}
_MULL_REV = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "rejections": [], "stop": False,
        "p0_seat": None, "p1_seat": None,
        "hold_priority": False,
        "vi_wired": set(),
        "cast_in_flight": None,
        "dos_cast": False, "dos_oid": None, "dos_cast_turn": None,
        "x_answered": False, "x_prompt_wired": False,
        "dos_stack_wired": False,
    })
    _MULL_REV.clear(); _PASSED_REV.clear(); _DISCARD_REV.clear(); WF_SEEN.clear()


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
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
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


def bf_oid(state, pid, name, exclude=()):
    for oid, o in bf(state, pid):
        if oname(o) == name and str(oid) not in {str(x) for x in exclude}:
            return oid
    return None


def bf_oids(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def life_of(state, pid):
    players = state.get("players") or []
    return (players[pid] or {}).get("life") if len(players) > pid else None


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action, "stage": ST.get("stage")})
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
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def export_now(c, path):
    try:
        s = await c.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


async def do_mulligan(c, pid, lands, key_names, need_counts=None):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    if _MULL_REV.get((c.name, c.revision)):
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    hand_names = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
    n_lands = sum(1 for n in hand_names if n in lands)
    has_key = any(n in key_names for n in hand_names)
    n_mulls = _MULL_REV.get(c.name, 0)
    keep_ok = (n_lands >= 2 and has_key) or n_mulls >= 2 or \
        (n_lands >= 3 and n_mulls >= 1)
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        _MULL_REV[c.name] = n_mulls + 1
    _MULL_REV[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
    return True


def _discard_order(state, pid, first_names, last_names):
    h = hand_oids(state, pid)

    def rank(o):
        n = oname(state["objects"][o])
        if n in first_names:
            return 0
        if n in last_names:
            return 2
        return 1
    return sorted(h, key=rank)


async def do_bottom(c, pid, lands, protect=()):
    st = c.latest
    if not st:
        return False
    if _MULL_REV.get((c.name, "bottom", c.revision)):
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    ph = pend.get("phase") or {}
    if ph.get("type") != "BottomCards":
        return False
    n = int(ph.get("count", 1) or 1)
    picks = [int(x) for x in _discard_order(state, pid, lands, protect)[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid, lands, protect=()):
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
    picks = [int(x) for x in _discard_order(state, pid, lands, protect)[:n]]
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} discards {len(picks)}")
    return True


async def pass_priority(c, pid):
    st = c.latest
    if not st:
        return False
    if ST.get("hold_priority"):
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
        wire("waiting_for", {"type": wf, "data": wf_data(state), "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} stage={ST.get('stage')}")


def vi_ops(st):
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def find_number_opp(st):
    """X-choice opportunity: schema with spec.type == 'number'."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if spec.get("type") == "number":
            return opp, spec.get("min"), spec.get("max")
    return None


async def answer_x(c, pid, st):
    """Answer the Day of Black Sun X prompt with X_VALUE, once."""
    if ST.get("x_answered"):
        return False
    found = find_number_opp(st)
    if not found:
        return False
    opp, mn, mx = found
    if not ST.get("x_prompt_wired"):
        ST["x_prompt_wired"] = True
        wire("x_prompt_seen", {"opp": opp, "min": mn, "max": mx,
                               "stage": ST.get("stage")})
        say(f"[x] prompt seen: min={mn} max={mx}")
    if mx is not None and X_VALUE > mx:
        wire("x_out_of_range", {"value": X_VALUE, "max": mx})
        say(f"[x] X={X_VALUE} above max {mx}; aborting cast")
        ST["stop"] = True
        return False
    iid = opp.get("interactionId")
    if not iid:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "number", "data": {"value": int(X_VALUE)}}}
    wire("x_submit", {"iid": iid, "value": X_VALUE, "min": mn, "max": mx})
    await c.send_interaction(sub)
    ST["x_answered"] = True
    say(f"[x] answered X={X_VALUE}")
    return True


async def handle_mana_payment(c, pid, state, st):
    if wf_type(state) != "ManaPayment":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    for a in st.get("legal_actions", []) or []:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            wire("mana_pay_action", {"who": c.name, "action": a,
                                     "stage": ST.get("stage")})
            await submit_as_is(c, a)
            say(f"[{c.name}] ManaPayment: submitted {a.get('type')}")
            return True
    return False


async def p_land_drop(c, pid, state, acts, land_name):
    if state.get("land_played_this_turn"):
        return False
    lid = find_hand(state, pid, land_name)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == str(lid):
                await submit_as_is(c, a)
                say(f"[{c.name}] land drop {land_name}")
                return True
    return False


async def p_cast(c, pid, state, acts, name, tag=None):
    oid = find_hand(state, pid, name)
    if not oid:
        return False
    if ST.get("cast_in_flight"):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            await submit_as_is(c, a)
            say(f"[{c.name}] cast {name} (oid={oid})")
            ST["cast_in_flight"] = str(oid)
            if tag:
                ST[tag] = str(oid)
            return True
    return False


def clear_inflight(state, pid):
    cif = ST.get("cast_in_flight")
    if cif and not any(str(oid) == str(cif) for oid in hand_oids(state, pid)):
        ST["cast_in_flight"] = None
        wire("cast_left_hand", {"oid": cif})
        say(f"cast in-flight {cif} left hand")


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P0_LANDS, (DOS,)):
        return True
    if await do_bottom(c, pid, P0_LANDS, protect=(DOS, MEMNITE)):
        return True
    if await do_discard(c, pid, P0_LANDS, protect=(DOS, MEMNITE)):
        return True
    if await answer_x(c, pid, st):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True

    stage = ST["stage"]

    if stage == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, MEMNITE):
            return True
        if await p_land_drop(c, pid, state, acts, SWAMP):
            return True
        # ready check
        mems0 = bf_oids(state, pid, MEMNITE)
        mems1 = bf_oids(state, 1, MEMNITE)
        angels1 = bf_oids(state, 1, ANGEL)
        stack_empty = len(state.get("stack") or []) == 0
        ready = (
            len(untapped_lands(state, pid, SWAMP)) >= 7
            and find_hand(state, pid, DOS)
            and len(mems0) >= 2
            and len(mems1) >= 1
            and len(angels1) >= 1
            and life_of(state, 0) == 20 and life_of(state, 1) == 20
            and stack_empty
            and not ST.get("pre_exported")
            and (state.get("turn_number") or 99) <= 80
        )
        if ready:
            s = await export_now(c, "pre.json")
            if s is not None:
                ST["pre_exported"] = True
                wire("pre", {"p0_memnites": mems0, "p1_memnites": mems1,
                             "p1_angels": angels1,
                             "untapped_swamps": untapped_lands(state, pid, SWAMP),
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase")})
                say(f"[P0] pre exported: {len(mems0)} P0 memnites, "
                    f"{len(mems1)} P1 memnites, {len(angels1)} P1 angels "
                    f"(turn {state.get('turn_number')})")
                ST["stage"] = "CAST"
            return True

    if stage == "CAST":
        if is_my_main(state, pid) and my_priority(state, pid):
            clear_inflight(state, pid)
            if not ST.get("dos_cast"):
                # X=5 + BB = 7 mana total
                if len(untapped_lands(state, pid, SWAMP)) >= 7:
                    if await p_cast(c, pid, state, acts, DOS, tag="dos_oid"):
                        ST["dos_cast"] = True
                        ST["dos_cast_turn"] = state.get("turn_number")
                        say(f"[P0] Day of Black Sun cast (oid {ST['dos_oid']})")
                        return True
            if await p_land_drop(c, pid, state, acts, SWAMP):
                return True

    # never attack; never block
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say("[P0] declares no blockers")
            return True
    if await pass_priority(c, pid):
        return True
    return False


async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P1_LANDS, (ANGEL,)):
        return True
    if await do_bottom(c, pid, P1_LANDS, protect=(ANGEL, MEMNITE)):
        return True
    if await do_discard(c, pid, P1_LANDS, protect=(ANGEL, MEMNITE)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True
    if is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, MEMNITE):
            return True
        # Serra Angel {3}{W}{W}: needs 5 untapped Plains
        if len(untapped_lands(state, pid, PLAINS)) >= 5:
            if await p_cast(c, pid, state, acts, ANGEL):
                return True
        if await p_land_drop(c, pid, state, acts, PLAINS):
            return True
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    if await pass_priority(c, pid):
        return True
    return False


def dos_on_stack(state):
    for se in state.get("stack") or []:
        blob = json.dumps(se, default=str)
        if DOS in blob:
            return se
    return None


def dos_in_gy(state, pid):
    for oid, o in state["objects"].items():
        if oname(o) == DOS and o.get("zone") == "Graveyard" \
                and o.get("owner") == pid:
            return oid
    return None


async def main():
    reset()
    t0 = time.time()
    os.environ["PHASE_WS_URL"] = "ws://127.0.0.1:9374/ws"
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ST["p0_seat"] = p0.player_id
    ST["p1_seat"] = p1.player_id
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p1_seat": p1.player_id,
                          "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                          "x_value": X_VALUE})
    clients = [(p0, p0.player_id, p0_tick), (p1, p1.player_id, p1_tick)]
    last_rev = {}
    last_wall = 0

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, _ in clients:
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(
                    {"at": now, "who": c.name, "type": r["type"], "data": r["data"]}
                    for r in rej)
                if ST.get("cast_in_flight"):
                    wire("cast_inflight_cleared_on_reject",
                         {"oid": ST["cast_in_flight"]})
                    ST["cast_in_flight"] = None
                    if ST.get("dos_oid"):
                        ST["dos_oid"] = None
                        ST["dos_cast"] = False
                if ST.get("x_answered"):
                    ST["x_answered"] = False
                    wire("x_flag_cleared_on_reject", {})
        if now - last_wall >= 3:
            last_wall = now
            for c, pid, tickf in clients:
                try:
                    await tickf(c, pid)
                except Exception as e:
                    say(f"tick error [{c.name}]: {e}")
            for c, _, _ in clients:
                last_rev[c.name] = c.revision
        else:
            for c, pid, tickf in clients:
                if c.revision != last_rev.get(c.name, -1):
                    try:
                        await tickf(c, pid)
                    except Exception as e:
                        say(f"tick error [{c.name}]: {e}")
                    last_rev[c.name] = c.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        _p0, _p1 = p0.player_id, p1.player_id
        stage = ST["stage"]
        turn = state.get("turn_number") or 0
        stack = state.get("stack") or []

        if stage == "CAST":
            # mid checkpoint: spell on the stack with X chosen
            if ST.get("x_answered") and ST.get("dos_cast") \
                    and not ST.get("mid_exported"):
                entry = dos_on_stack(state)
                if entry:
                    s = await export_now(p0, "mid.json")
                    if s is not None:
                        ST["mid_exported"] = True
                        wire("dos_on_stack", {"entry": entry, "turn": turn,
                                             "x": X_VALUE})
                        say(f"[P0] mid exported: DoBS on stack (turn {turn})")
            # post checkpoint: resolved -> graveyard, stack empty
            if ST.get("dos_cast") and not ST.get("post_exported"):
                gy = dos_in_gy(state, _p0)
                if gy and len(stack) == 0:
                    s = await export_now(p0, "post.json")
                    if s is not None:
                        ST["post_exported"] = True
                        wire("post", {"turn": turn, "stack_n": 0,
                                      "dos_gy_oid": gy,
                                      "life": [life_of(state, 0), life_of(state, 1)]})
                        say(f"[P0] post exported: DoBS resolved to GY (turn {turn})")
                        ST["stage"] = "DONE"
                        ST["stop"] = True

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        if now - ST.get("last_diag", 0) > 30:
            ST["last_diag"] = now
            hand0 = [oname(state["objects"][o]) for o in hand_oids(state, _p0)]
            p1st = p1.latest
            hand1 = ([oname(p1st["state"]["objects"][o]) for o in hand_oids(p1st["state"], _p1)]
                     if p1st else ["?"])
            nbf0 = len(bf(state, _p0))
            nbf1 = len(bf(state, _p1))
            say(f"DIAG turn={turn} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"life={[life_of(state,0), life_of(state,1)]} "
                f"p0hand={hand0[:8]} p1hand={hand1[:8]} bf={nbf0}/{nbf1} stage={stage}")
            wire("diag", {"turn": turn, "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "life": [life_of(state, 0), life_of(state, 1)],
                          "p0hand": hand0, "p1hand": hand1,
                          "nbf0": nbf0, "nbf1": nbf1, "stage": stage})

        if not ST.get("pre_exported") and now - t0 > 900:
            say("SETUP stalled 900s without pre export; stopping")
            wire("setup_stalled", {})
            ST["stop"] = True
        if not ST.get("post_exported") and now - t0 > TIMEOUT - 120:
            await export_now(p0, "post.json")
            ST["post_exported"] = True
            ST["stop"] = True

    await p0.close()
    await p1.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list, set)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
