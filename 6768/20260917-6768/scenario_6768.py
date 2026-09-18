#!/usr/bin/env python3
"""Issue #6768: Bruna and Gisela don't merge. -- they just exile themselves.

Reported (Discord): "they just exile thmeselves."
[[gisela, the broken blade]][[bruna the fading light]]

Oracle (pinned v0.86.0 card-data.json):
  Gisela, the Broken Blade (3W, 4/3, Flying/first strike/lifelink):
    "At the beginning of your end step, if you both own and control Gisela
     and a creature named Bruna, the Fading Light, exile them, then meld
     them into Brisela, Voice of Nightmares."
  Bruna, the Fading Light (5WW, 5/7, Flying/vigilance, meld partner):
    "When you cast this spell, you may return target Angel or Human
     creature card from your graveyard to the battlefield."

Parser state in pinned data: the end-step trigger is parsed with a
{"type": "Meld", "source": "Gisela, the Broken Blade",
 "partner": "Bruna, the Fading Light", "result": "Brisela, Voice of
 Nightmares"} effect (supported), with a condition requiring P0 to own and
control both. Prior run on v0.79.0 (protocol 69, 2026-09-11): the exile half
fired but the combined object was never created (verdict reproduced); that
run is stale relative to the pinned v0.86.0, so this run re-validates on
protocol 72.

Behavioral contract (single game, two human-client seats, v0.86.0/proto 72):
  RAMP   - P0 plays Plains, casts Gisela (needs 4 untapped Plains), then
           Bruna (needs 7). Bruna's cast trigger (may return from gy) is
           DECLINED via viewer_interaction (exactChoices -> choose/false).
           No attacks (empty declarations).
  PRE    - at P0's PostCombatMain with both on P0's battlefield:
           export pre.json, stage -> OBSERVE.
  OBSERVE- let the end-step trigger run its course; log stack entries;
           export mid_trigger.json while a Meld/Gisela/Brisela entry is on
           the stack; export post_trigger.json once the stack empties in
           EndStep/Cleanup; export post.json when P1's turn begins.
  STOP   - stop after post.json.

  A1 setup_ok       pre.json: Gisela + Bruna on P0 BF, life 20/20.
  A2 trigger_fires  Meld trigger observed (stack entry or
                    triggers_fired_this_turn mentioning Meld/Gisela).
  A3 exile_observed both halves leave the battlefield for Exile around the
                    trigger resolution.
  A4 meld_correct   Brisela, Voice of Nightmares on P0's battlefield after
                    resolution (the reported outcome is its absence).
  A5 cleanup        stack empty in post.json; turn advanced to P1.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and A4 fails.
Verdict = not-reproduced iff A1..A5 all pass.
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
RUN_ID = "20260917-6768"
EVID_ISSUE = "6768"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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


BRUNA = "Bruna, the Fading Light"
GISELA = "Gisela, the Broken Blade"
BRISELA = "Brisela, Voice of Nightmares"

P0_DECK = deck((BRUNA, 12), (GISELA, 12), ("Plains", 36))
P1_DECK = deck(("Forest", 60))

ST = {"stage": "RAMP", "stop": False, "pre_turn": None,
      "mid_exported": False, "post_trigger_exported": False,
      "stall_since": None}
WF_SEEN = []
SUBMITTED = {}  # iid -> (submit_time, stage) for interaction retry
MULL_ANSWERED = set()  # client names (MulliganDecision gate per seat)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def pending_mulligan(state, pid):
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "MulliganDecision":
        return False
    return any(p.get("player") == pid
               and (p.get("phase") or {}).get("type") == "Declare"
               for p in (wf.get("data") or {}).get("pending") or [])


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


async def discard_to_hand_size(c, st, pid):
    """Answer DiscardToHandSize for my seat via viewer_interaction select;
    fall back to advertised SelectCards with cardIds. True if acted."""
    state = st.get("state") or {}
    wf = state.get("waiting_for") or {}
    data = wf.get("data") or {}
    if wf.get("type") != "DiscardToHandSize" or data.get("player") != pid:
        return False
    oids = hand_oids(state, pid)
    n = data.get("count") or max(0, len(oids) - 7)
    seen = set()

    def rank(oid):
        nm = lname(objs(state)[oid])
        if nm == "plains":
            return 0
        if nm in (BRUNA.lower(), GISELA.lower()):
            if nm in seen:
                return 1
            seen.add(nm)
        return 2
    picks = sorted(oids, key=rank)[:n]
    if not picks:
        return False
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rdata = resp.get("data") or {}
            spec = rdata.get("spec") or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            iid = opp.get("interactionId")
            if iid in SUBMITTED:
                continue
            chs = rdata.get("choices") or rdata.get("candidates") or []
            ref_to_id = {}
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    ref = d.get("reference")
                    if ref is not None:
                        ref_to_id[str(ref)] = ch.get("id")
            want = [ref_to_id[p] for p in picks if p in ref_to_id]
            if want and stype:
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": stype,
                                     "data": {"choiceIds": want}}})
                SUBMITTED[iid] = (time.time(), ST["stage"])
                say(f"[{c.name}] discards {len(want)} via {stype} "
                    f"(candidates matched by reference)")
                return True
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "SelectCards":
            sub = copy.deepcopy(a)
            d = sub.setdefault("data", {})
            d["cardIds"] = [int(x) for x in picks]
            wire("action_submit", {"who": c.name, "action": "SelectCards/discard",
                                   "picks": picks})
            await c.send_action(sub)
            say(f"[{c.name}] discards {len(picks)} via SelectCards")
            return True
    say(f"[{c.name}] DiscardToHandSize: no answer path!")
    return False


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, key):
    out = []
    for oid, o in objs(state).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        if lname(o) == key.lower():
            out.append((int(oid), o))
    return out


def exile_named(state, key):
    out = []
    for oid, o in objs(state).items():
        if o.get("zone") != "Exile":
            continue
        if lname(o) == key.lower():
            out.append((int(oid), o))
    return out


def untapped_land_count(state, pid):
    return sum(
        1 for oid, o in objs(state).items()
        if o.get("zone") == "Battlefield" and o.get("controller") == pid
        and "Land" in ((o.get("base_card_types") or {}).get("core_types") or [])
        and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def life_of(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid))
        return (pp or {}).get("life") if isinstance(pp, dict) else None
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p.get("life")
    return None


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a
    return None


def stack_desc(state):
    out = []
    for sid in state.get("stack", []) or []:
        o = objs(state).get(str(sid), {})
        out.append({"id": sid, "name": oname(o),
                    "zone": o.get("zone"),
                    "kind": str(o.get("kind", ""))[:80]})
    return out


def meldish(blob):
    b = str(blob).lower()
    return ("meld" in b or "gisela" in b or "brisela" in b
            or "bruna" in b)


def choice_decline(ch):
    """True if this exactChoices candidate means decline/false/no."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        role = str(d.get("role", "")).lower()
        val = str(d.get("value", "")).lower()
        code = str(d.get("code", "")).lower()
        if val == "false":
            return True
        if "decline" in code or "decline" in role:
            return True
        if str(ch.get("id", "")).lower().startswith("no"):
            return True
    txt = str(ch.get("text", "") or "").lower()
    if txt in ("no", "decline", "don't", "do not"):
        return True
    return False


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf})


def export_now(path, state_str):
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(state_str)
    say(f"exported {path}")


C0 = None


async def do_export(path):
    s = await C0.export_state()
    export_now(path, s)
    return json.loads(s)["state"]


async def decline_may(c):
    """Decline Bruna's cast trigger (may-return) via viewer_interaction.

    Protocol 72: exactChoices opportunities answer with type "choose" +
    singular choiceId."""
    st = c.latest
    vi = get_vi(st) if st else None
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data") or {}
        cands = rdata.get("choices") or rdata.get("candidates") or []
        if rtype != "exactChoices" or not cands:
            continue
        for ch in cands:
            if choice_decline(ch):
                iid = opp.get("interactionId")
                if iid in SUBMITTED:
                    continue
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch["id"]}}}
                await send_interaction(c, sub)
                SUBMITTED[iid] = (time.time(), ST["stage"])
                say(f"[{c.name}] declines Bruna may-return (choice {ch['id']})")
                return True
    return False


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    # mulligan: always keep (gate on my seat's pending phase, once per client)
    if pending_mulligan(state, pid) and c.name not in MULL_ANSWERED:
        for a in acts:
            if a["type"] == "MulliganDecision":
                wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
                await c.send_action(a)
                MULL_ANSWERED.add(c.name)
                say("[P0] keeps")
                return True

    # discard to hand size (protocol-72 interaction path)
    if await discard_to_hand_size(c, st, pid):
        return True

    # Bruna's cast trigger: decline the may-return
    if wtype == "OptionalEffectChoice" and wplayer == 0:
        if await decline_may(c):
            return True
        wire("may_decline_noopportunity", {"wtype": wtype})
        return False

    # declare attackers: always empty (no combat needed)
    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    # declare blockers: none
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    # mana payments: submit as-is (engine auto-taps on protocol 72)
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    # never pass priority while a P0 cast decision is pending
    if wtype in ("OptionalCostChoice", "TargetSelection", "ManaPayment") \
            and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    if is_my_main(state, pid) and ST["stage"] == "RAMP":
        # land drop (retry every tick; no kept-flags)
        hid = find_hand(state, pid, "Plains")
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand",
                                       "land": "plains"})
                await c.send_action(a)
                say("[P0] plays Plains")
                return True
        # cast Gisela (3W; attempt when 4+ untapped lands), one copy only
        gisela_bf = bf_named(state, 0, GISELA)
        gid = find_hand(state, pid, GISELA)
        if not gisela_bf and gid and untapped_land_count(state, pid) >= 4:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == gid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Gisela"})
                    await c.send_action(a)
                    say("[P0] casts Gisela, the Broken Blade")
                    return True
        # cast Bruna (5WW; attempt when 7+ untapped lands), one copy only
        bruna_bf = bf_named(state, 0, BRUNA)
        bid = find_hand(state, pid, BRUNA)
        if not bruna_bf and bid and untapped_land_count(state, pid) >= 7:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == bid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Bruna"})
                    await c.send_action(a)
                    say("[P0] casts Bruna, the Fading Light")
                    return True

    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    if pending_mulligan(state, pid) and c.name not in MULL_ANSWERED:
        for a in acts:
            if a["type"] == "MulliganDecision":
                await c.send_action(a)
                MULL_ANSWERED.add(c.name)
                return True
    if await discard_to_hand_size(c, st, pid):
        return True
    da = find_action(acts, "DeclareAttackers")
    if da:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        hid = find_hand(state, pid, "forest")
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------------ main

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(P0_DECK)
    except Exception as e:
        say(f"game creation failed: {e}")
        A["A1_setup_ok"] = "blocked"
        obs["notes"].append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN}, f, indent=2)
        await p0.close()
        return obs
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    global C0
    C0 = p0

    last_rev = {}
    stall_watch = {}  # client name -> (rev, since)
    TIMEOUT = 1500
    last_stack_sig = None
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        # interaction retry: a rejected interaction moves no revision; allow
        # the iid to be resubmitted after ~15s of no revision advance
        now = time.time()
        for iid in [i for i, (t, _) in SUBMITTED.items() if now - t > 15]:
            del SUBMITTED[iid]
            wire("interaction_retry_reset", {"iid": iid})
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        # stale-client watchdog: log view when revision >45s without advancing
        for c in (p0, p1):
            rev = c.revision
            prev = stall_watch.get(c.name)
            if prev and prev[0] == rev and now - prev[1] > 45:
                st = c.latest or {}
                state = st.get("state") or {}
                say(f"[watchdog] {c.name} rev={rev} stale>45s "
                    f"turn={state.get('turn_number')} phase={state.get('phase')} "
                    f"wf={(state.get('waiting_for') or {}).get('type')} "
                    f"pp={state.get('priority_player')}")
                wire("stale_client", {"who": c.name, "rev": rev})
                stall_watch[c.name] = (rev, now)  # re-arm
            elif not prev or prev[0] != rev:
                stall_watch[c.name] = (rev, now)
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # RAMP -> OBSERVE: both halves on P0 BF at PostCombatMain
        if ST["stage"] == "RAMP" \
                and state.get("active_player") == 0 \
                and (state.get("phase") or "") == "PostCombatMain" \
                and bf_named(state, 0, GISELA) and bf_named(state, 0, BRUNA):
            pre = await do_export("pre.json")
            ST["pre_turn"] = pre.get("turn_number")
            wire("pre_export", {"gisela": [o for o, _ in bf_named(pre, 0, GISELA)],
                                "bruna": [o for o, _ in bf_named(pre, 0, BRUNA)],
                                "life": [life_of(pre, 0), life_of(pre, 1)]})
            ST["stage"] = "OBSERVE"
            say(f"[pre] both halves on P0 BF at turn {ST['pre_turn']} "
                f"PostCombatMain")

        if ST["stage"] == "OBSERVE":
            # stack watch: log any meld-shaped entries (sampled every iteration)
            sd = stack_desc(state)
            sig = json.dumps(sd, sort_keys=True, default=str)
            if sig != last_stack_sig:
                last_stack_sig = sig
                hits = [e for e in sd if meldish(json.dumps(e, default=str))]
                wire("stack", {"entries": sd,
                               "triggers_fired": state.get("triggers_fired_this_turn")})
                if hits:
                    say(f"[stack meld] {json.dumps(hits)[:400]}")

            # mid export: meld trigger on the stack
            if not ST["mid_exported"] and any(
                    meldish(json.dumps(e, default=str)) for e in sd):
                mid = await do_export("mid_trigger.json")
                ST["mid_exported"] = True
                wire("mid_trigger", {"phase": mid.get("phase"),
                                     "turn": mid.get("turn_number"),
                                     "stack": stack_desc(mid)})
                say(f"[mid] trigger on stack at phase={mid.get('phase')}")

            # stall watchdog: stack stuck >90s during OBSERVE
            if state.get("stack"):
                if ST["stall_since"] is None:
                    ST["stall_since"] = time.time()
                elif time.time() - ST["stall_since"] > 90:
                    await do_export("mid_stall.json")
                    obs["notes"].append(
                        "stall watchdog fired: stack non-empty >90s during OBSERVE")
                    say("[stall] watchdog fired; capturing mid_stall.json")
                    ST["stop"] = True
            else:
                ST["stall_since"] = None

            # post-trigger export: capture from the stack-emptying tick itself
            # (per #6768 lesson: the game advances fast after resolution, so
            # gate on stack-empty in EndStep/Cleanup, not on P1's turn)
            if ST["mid_exported"] and not ST["post_trigger_exported"] \
                    and not (state.get("stack") or []) \
                    and (state.get("phase") or "") in ("EndStep", "Cleanup"):
                await asyncio.sleep(0.5)
                ptr = await do_export("post_trigger.json")
                ST["post_trigger_exported"] = True
                wire("post_trigger",
                     {"phase": ptr.get("phase"), "turn": ptr.get("turn_number"),
                      "bf_brisela": [o for o, _ in bf_named(ptr, 0, BRISELA)],
                      "exile_bruna": [o for o, _ in exile_named(ptr, BRUNA)],
                      "exile_gisela": [o for o, _ in exile_named(ptr, GISELA)]})
                say(f"[post_trigger] phase={ptr.get('phase')} "
                    f"brisela_bf={len(bf_named(ptr, 0, BRISELA))}")

            # post export: P1's turn begins (game advanced past resolution)
            if ST["post_trigger_exported"] and state.get("active_player") == 1:
                await asyncio.sleep(1.0)
                post = await do_export("post.json")
                wire("post", {"phase": post.get("phase"),
                              "turn": post.get("turn_number"),
                              "bf_brisela": [o for o, _ in bf_named(post, 0, BRISELA)]})
                say(f"[post] phase={post.get('phase')} turn={post.get('turn_number')}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def load_env(p):
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)

    def env_state(p):
        try:
            return load_env(p)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    mid = env_state("mid_trigger.json")
    ptr = env_state("post_trigger.json")
    post = env_state("post.json")

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre.json was never exported (both halves never "
                            "on BF at PostCombatMain)")
    else:
        g = bf_named(pre, 0, GISELA)
        b = bf_named(pre, 0, BRUNA)
        ok = (len(g) >= 1 and len(b) >= 1
              and life_of(pre, 0) == 20 and life_of(pre, 1) == 20)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"pre: gisela_bf={len(g)} bruna_bf={len(b)} "
                            f"life={[life_of(pre, 0), life_of(pre, 1)]}")

    # A2: trigger fired (stack entry or triggers_fired_this_turn)
    trig_hits = []
    for src, s in (("mid", mid), ("ptr", ptr), ("post", post)):
        if s is None:
            continue
        for entry in s.get("triggers_fired_this_turn") or []:
            blob = json.dumps(entry, default=str)
            if meldish(blob):
                trig_hits.append(f"{src}:triggers_fired:{blob[:160]}")
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "stack":
            for e in d["payload"].get("entries", []) or []:
                if meldish(json.dumps(e, default=str)):
                    trig_hits.append("stack:" + str(e.get("name")))
                    break
    trig_hits = list(dict.fromkeys(trig_hits))
    A["A2_trigger_fires"] = "passed" if trig_hits else "failed"
    obs["notes"].append(f"trigger evidence hits: {trig_hits[:4] or 'none'}")

    # A3: both halves exiled around resolution
    ex_states = [s for s in (ptr, post) if s is not None]
    if ex_states:
        s = ex_states[0]
        eb, eg = exile_named(s, BRUNA), exile_named(s, GISELA)
        bb, bg = bf_named(s, 0, BRUNA), bf_named(s, 0, GISELA)
        A["A3_exile_observed"] = "passed" if (eb or eg) and not bb and not bg \
            else "failed"
        obs["notes"].append(f"resolution state: exile bruna={len(eb)} "
                            f"gisela={len(eg)}; bf bruna={len(bb)} gisela={len(bg)}")
    else:
        A["A3_exile_observed"] = "not-run"
        obs["notes"].append("no post-resolution state; A3 not-run")

    # A4: Brisela on P0 battlefield after resolution
    if ex_states:
        br = [bf_named(s, 0, BRISELA) for s in ex_states]
        found = any(br)
        A["A4_meld_correct"] = "passed" if found else "failed"
        obs["notes"].append(
            f"brisela_bf={[len(x) for x in br]} "
            f"(expected 1 melded permanent under P0)")
        if not found:
            for s in ex_states:
                zones = {}
                for o in (s.get("objects", {}) or {}).values():
                    if lname(o) in (BRUNA.lower(), GISELA.lower(),
                                    BRISELA.lower()):
                        zones.setdefault(o.get("zone"), []).append(lname(o))
                obs["notes"].append(f"half/brisela zones: {zones}")
    else:
        A["A4_meld_correct"] = "not-run"
        obs["notes"].append("no post-resolution state; A4 not-run")

    # A5
    if post is not None:
        empty = not (post.get("stack") or [])
        advanced = (post.get("turn_number") or 0) > (ST.get("pre_turn") or 0) \
            or post.get("active_player") == 1
        A["A5_cleanup"] = "passed" if (empty and advanced) else "failed"
        obs["notes"].append(f"post: phase={post.get('phase')} "
                            f"turn={post.get('turn_number')} "
                            f"stack_empty={empty} advanced={advanced}")
    else:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append("post.json missing; A5 not-run")

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2)

    # run.json
    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if A.get("A4_meld_correct") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6768,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.86.0",
            "build_commit": "2cc8c28",
            "protocol_version": 72,
            "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
            "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c0f4af30a9c40ff26",
            "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Bruna, the Fading Light": 12, "Gisela, the Broken Blade": 12,
                   "Plains": 36},
            "P1": {"Forest": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6768.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
