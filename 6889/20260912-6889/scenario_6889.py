#!/usr/bin/env python3
"""Issue #6889: Tolsimir, Friend to Wolves isn't triggering all effects.

Oracle text: "When Tolsimir enters, create Voja, Friend to Elves, a legendary
3/3 green and white Wolf creature token. Whenever a Wolf you control enters,
you gain 3 life and that creature fights up to one target creature you don't
control."

Reported: the life gain works, but the fight option ("fights up to one target
creature you don't control") doesn't appear to trigger or resolve.

v0.80.0 card-data parse: the Wolf-enter trigger's execute is GainLife 3 ONLY
-- no fight sub-ability, no target. The fight clause is absent at the data
layer too (triage agrees: "its child structure contains only GainLife").

Plan (native engine, v0.80.0 / protocol 69, two human-driver seats):
  P0 casts Tolsimir, Friend to Wolves ({2}{G}{G}{W}).
  P1 fields 2x Grizzly Bears (legal fight targets; 2+ forces a real prompt
  instead of single-target auto-targeting).
  Watch the Wolf-enter trigger: expect +3 life, then a TargetSelection /
  TriggerTargetSelection prompt for the fight. If offered, target a Bear and
  verify the fight (damage on both). If no prompt and no fight while the
  trigger otherwise resolves -> reproduced.

Behavioral contract:
  A1 setup_ok      Tolsimir on P0 BF, Voja token on P0 BF, >=2 Bears on P1 BF
  A2 wolf_trigger  Wolf-enter TriggeredAbility observed on the stack
  A3 life_gain     P0 life +3 across the trigger window
  A4 fight_prompted target prompt for the fight offered (expected FAILED)
  A5 fight_resolved Voja/Bear fight damage observed (not-run when no prompt)
  A6 cleanup       stack empty, game proceeding after the trigger

Verdict: reproduced iff A1+A2+A3 pass and A4 fails.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6889"
EVDIR = f"{BACKFILL}/evidence/6889/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TOLS = "Tolsimir, Friend to Wolves"
BEAR = "Grizzly Bears"
FOREST = "Forest"
PLAINS = "Plains"
P0_DECK = [(TOLS, 12), (FOREST, 24), (PLAINS, 24)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]
TIMEOUT = 1500

ST = {}
WF_SEEN = []
SUBMITTED_IIDS = set()


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",       # SETUP -> TRIGGER -> DONE
        "stop": False,
        "rejections": [],
        "turn_cap": 30,
        "server_hello": None,
        "pre_life": None,
        "mid_exported": False,
        "post_exported": False,
        "tols_cast": False,
        "voja_seen": False,
        "wolf_trigger_seen": False,
        "wolf_trigger_resolved": False,
        "fight_prompted": False,
        "fight_answered": False,
        "fight_target_oid": None,
        "life_after_trigger": None,
        "trigger_stack_ids": [],
    })
    WF_SEEN.clear()
    SUBMITTED_IIDS.clear()


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
        WIRE.write(json.dumps({"t": time.time(), "event": event,
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


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def bf_name_contains(state, pid, frag):
    return [(oid, o) for oid, o in bf(state, pid)
            if frag.lower() in oname(o).lower()]


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


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf
               or WF_SEEN[-1][2] != ST["stage"]):
        WF_SEEN.append((wf, wf_player(state), ST["stage"]))
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST["stage"]})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST['stage']}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


async def submit_interaction(c, iid, response, why):
    wire("interaction_submit", {"who": c.name, "why": why,
                                "interactionId": iid, "response": response,
                                "stage": ST.get("stage")})
    await c.send_interaction({"interactionId": iid, "response": response})


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


async def export_now(path):
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


def playland_advertised(acts, oid):
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def stack_entries(state):
    return state.get("stack") or []


def wolf_trigger_on_stack(state):
    """Return stack entries that are the Wolf-enter trigger (source Tolsimir,
    description mentions Wolf + gain 3 life)."""
    out = []
    for e in stack_entries(state):
        kind = (e.get("kind") or {})
        if kind.get("type") != "TriggeredAbility":
            continue
        desc = ""
        # protocol 69 nests the ability under kind.data.ability
        for holder in (e.get("ability"),
                       (kind.get("data") or {}).get("ability")):
            if isinstance(holder, dict) and holder.get("description"):
                desc = str(holder.get("description"))
                break
        desc = desc or str(e.get("description") or "")
        src = str(e.get("source_id") or e.get("source") or "")
        if "wolf" in desc.lower() and "gain 3 life" in desc.lower():
            out.append((e.get("id"), desc, src))
    return out


def observe_trigger(state):
    """Scan the stack for the Wolf-enter trigger; update ST. Called every
    tick on the freshest state."""
    for sid, desc, src in wolf_trigger_on_stack(state):
        if not ST["wolf_trigger_seen"]:
            ST["wolf_trigger_seen"] = True
            say(f"wolf trigger ON STACK id={sid}: {desc[:120]}")
        if sid not in ST["trigger_stack_ids"]:
            ST["trigger_stack_ids"].append(sid)
            wire("wolf_trigger_stack", {"id": sid, "desc": desc, "src": src,
                                        "stage": ST["stage"]})
    # resolution: trigger was seen, now gone from the stack
    if ST["wolf_trigger_seen"] and not ST["wolf_trigger_resolved"]:
        cur_ids = {sid for sid, _, _ in wolf_trigger_on_stack(state)}
        if not any(s in cur_ids for s in ST["trigger_stack_ids"]):
            ST["wolf_trigger_resolved"] = True
            ST["life_after_trigger"] = life(state, 0)
            say(f"wolf trigger RESOLVED; P0 life now "
                f"{ST['life_after_trigger']}")
            wire("wolf_trigger_resolved",
                 {"life": ST["life_after_trigger"], "stage": ST["stage"]})


def candidate_info(opp, state):
    """Extract (choice_id, name, zone, ref_oid) for schema candidates."""
    out = []
    data = (opp.get("response") or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        cid = ch.get("id") or ch.get("choiceId")
        ref = None
        for s in ch.get("surfaces", []) or []:
            sd = s.get("data") or {}
            if "reference" in sd:
                ref = sd["reference"]
        name, zone = "?", "?"
        if ref is not None:
            o = state["objects"].get(str(ref), {})
            name, zone = oname(o), o.get("zone")
        out.append({"choice_id": cid, "name": name, "zone": zone,
                    "ref": ref})
    return out


async def answer_fight_target(c, state, acts, st):
    """If the fight target prompt is up for P0, target a P1 Bear."""
    if ST["fight_answered"]:
        return False
    if wf_type(state) not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    if wf_player(state) != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if not iid or iid in SUBMITTED_IIDS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        spec = (resp.get("data") or {}).get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        cands = candidate_info(opp, state)
        if not cands:
            continue
        # fight targets: creatures on the battlefield we don't control
        bf_cands = [x for x in cands if x["zone"] == "Battlefield"]
        if not bf_cands:
            continue
        want = next((x for x in bf_cands if x["name"] == BEAR), bf_cands[0])
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            wire("fight_prompt_unexpected_shape",
                 {"rtype": rtype, "spec_type": spec_type,
                  "candidates": cands, "opportunity": opp})
            say(f"[P0] fight prompt unexpected shape {rtype}/{spec_type}; "
                f"holding")
            continue
        ST["fight_prompted"] = True
        wire("fight_target_prompt",
             {"rtype": rtype, "spec_type": spec_type, "candidates": cands,
              "want": want, "stage": ST["stage"]})
        say(f"[P0] FIGHT PROMPT seen; targeting {want['name']} "
            f"(ref {want['ref']})")
        await submit_interaction(c, iid, resp_out, "fight_target")
        SUBMITTED_IIDS.add(iid)
        ST["fight_answered"] = True
        ST["fight_target_oid"] = want["ref"]
        return True
    return False


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    if is_my_main(state, pid):
        # land drop
        for land in (FOREST, PLAINS):
            lid = find_hand(state, pid, land)
            a = playland_advertised(acts, lid) if lid else None
            if a:
                await submit_as_is(c, a)
                say(f"[P0] plays {land}")
                return True
        # cast Tolsimir once (legend; never double-cast)
        if ST["stage"] == "SETUP" and not bf_named(state, pid, TOLS):
            oid = find_hand(state, pid, TOLS)
            a = castspell_advertised(acts, oid)
            if a:
                if not ST["pre_life"]:
                    ST["pre_life"] = life(state, pid)
                    await export_now("pre.json")
                    wire("pre_life", {"life": ST["pre_life"]})
                await submit_as_is(c, a)
                ST["tols_cast"] = True
                say(f"[P0] casts {TOLS}")
                return True
    return False


async def p1_tick(c, pid, state, acts):
    if is_my_main(state, pid):
        lid = find_hand(state, pid, FOREST)
        a = playland_advertised(acts, lid) if lid else None
        if a:
            await submit_as_is(c, a)
            say("[P1] plays Forest")
            return True
        # field 2 bears during SETUP, then hold (keep board readable)
        if ST["stage"] == "SETUP" and len(bf_named(state, pid, BEAR)) < 2:
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P1] casts Grizzly Bears")
                return True
    return False


async def empty_declare(c, acts):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def empty_blockers(c, acts):
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    record_wf(state)
    drain_rejections(c)
    observe_trigger(state)
    # Voja token arrival -> mid export
    if not ST["mid_exported"] and bf_name_contains(state, 0, "Voja"):
        ST["voja_seen"] = True
        if ST["stage"] == "SETUP":
            ST["stage"] = "TRIGGER"
        await export_now("mid_trigger.json")
        ST["mid_exported"] = True
        wire("voja_seen", {"turn": state.get("turn_number"),
                           "phase": state.get("phase")})
        say(f"Voja token on P0 battlefield at turn "
            f"{state.get('turn_number')} {state.get('phase')}")
    # legend rule: never double-cast; handle ChooseLegend defensively
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True
    # mulligan: always keep
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{c.name}] keeps")
            return True
    # discard to hand size (named player)
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            say(f"[{c.name}] discards {n}")
            return True
    # fight target prompt (P0 only); never pass priority past it
    if pid == 0 and await answer_fight_target(c, state, acts, st):
        return True
    # never pass priority while a target/cost prompt names us
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt in ("TargetSelection", "TriggerTargetSelection",
                            "OptionalCostChoice", "ChooseManaColor",
                            "CombatTaxPayment"):
        wire("named_prompt_held", {"wf": wt, "who": c.name,
                                   "stage": ST["stage"]})
        say(f"[{c.name}] holding: {wt} names P{pid}, no handler fired")
        return True
    # declare attackers: nobody attacks (keeps life totals clean)
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if await empty_declare(c, acts):
            return True
    # declare blockers: empty
    if (state.get("phase") or "") == "DeclareBlockers":
        if await empty_blockers(c, acts):
            return True
    # payment prompts: submit as-is (engine auto-taps mana for casts)
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # main-phase driving
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def damage_marked(state, oid):
    o = state["objects"].get(str(oid), {})
    return o.get("damage_marked") or o.get("damage") or 0


async def main():
    reset()
    global C0, C1
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])
    # parse evidence: the two Tolsimir triggers from the pinned dataset
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.80.0/data/card-data.json"))
        tc = cd["tolsimir, friend to wolves"]
        with open(f"{EVDIR}/parse_evidence.json", "w") as f:
            json.dump({"card": tc["name"],
                       "oracle_text": tc["oracle_text"],
                       "triggers": tc["triggers"]}, f, indent=1)
        say("parse_evidence.json written")
    except Exception as e:
        say(f"parse evidence FAILED: {e}")
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        # post-export gate: trigger seen+resolved, life recorded, stack
        # empty, game has moved on -> capture post.json and stop
        st0 = C0.latest
        if st0:
            state = st0.get("state")
            # fallback: trigger resolved between stack observations --
            # detect via the +3 life gain after Voja entered
            if ST["voja_seen"] and not ST["wolf_trigger_seen"] \
                    and not ST["wolf_trigger_resolved"] \
                    and ST["pre_life"] is not None:
                if life(state, 0) == ST["pre_life"] + 3:
                    ST["wolf_trigger_seen"] = True
                    ST["wolf_trigger_resolved"] = True
                    ST["life_after_trigger"] = life(state, 0)
                    wire("wolf_trigger_implicit",
                         {"note": "life +3 observed without stack sighting",
                          "life": ST["life_after_trigger"]})
                    say("wolf trigger resolved implicitly (life +3, no "
                        "stack sighting)")
            if ST["wolf_trigger_resolved"] and not ST["post_exported"] \
                    and not stack_entries(state):
                await export_now("post.json")
                ST["post_exported"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("post.json exported; stopping")
        if acted0 or acted1:
            last_progress = time.time()
        st0 = C0.latest
        if st0 and (st0.get("state", {}).get("turn_number") or 0) > \
                ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if time.time() - last_progress > 180:
            say("no progress for 180s; dumping waiting_for and stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": (st.get("state", {}).get("waiting_for")),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST["stage"]})
            break
        await asyncio.sleep(0.15)

    # ---- assertions ----
    A = {}
    D = {}
    st0 = C0.latest.get("state") if C0.latest else {}

    def load_env(fn):
        try:
            return json.loads(open(f"{EVDIR}/{fn}").read())
        except Exception as e:
            D[f"{fn}_err"] = str(e)[:120]
            return None

    pre = load_env("pre.json")
    mid = load_env("mid_trigger.json")
    post = load_env("post.json")

    def env_state(env):
        if not env:
            return None
        s = env.get("state")
        return s if isinstance(s, dict) else json.loads(s)

    pre_s, mid_s, post_s = env_state(pre), env_state(mid), env_state(post)

    tols_bf = len(bf_named(mid_s, 0, TOLS)) > 0 if mid_s else False
    voja_bf = len(bf_name_contains(mid_s, 0, "Voja")) > 0 if mid_s else False
    bears_mid = len(bf_named(mid_s, 1, BEAR)) if mid_s else 0
    A["A1_setup_ok"] = "passed" if (tols_bf and voja_bf
                                    and bears_mid >= 2) else "failed"
    D["A1_setup_ok_detail"] = (f"tolsimir_bf={tols_bf} voja_bf={voja_bf} "
                               f"p1_bears_mid={bears_mid}")

    A["A2_wolf_trigger"] = "passed" if ST["wolf_trigger_seen"] else "failed"
    D["A2_wolf_trigger_detail"] = (
        f"trigger_stack_ids={ST['trigger_stack_ids']} "
        f"resolved={ST['wolf_trigger_resolved']}")

    pre_life = ST["pre_life"]
    post_life = ST["life_after_trigger"]
    if pre_life is None and pre_s:
        pre_life = life(pre_s, 0)
    if post_life is None and post_s:
        post_life = life(post_s, 0)
    gained = (post_life - pre_life) if (pre_life is not None
                                        and post_life is not None) else None
    A["A3_life_gain"] = "passed" if gained == 3 else (
        "failed" if gained is not None else "not-run")
    D["A3_life_gain_detail"] = (f"P0 life pre={pre_life} post-trigger="
                                f"{post_life} (expect +3)")

    A["A4_fight_prompted"] = "passed" if ST["fight_prompted"] else "failed"
    D["A4_fight_prompted_detail"] = (
        f"fight_prompted={ST['fight_prompted']} "
        f"fight_answered={ST['fight_answered']} "
        f"target_oid={ST['fight_target_oid']}; waiting_for types seen: "
        f"{[w[0] for w in WF_SEEN]}")

    # A5: fight damage — only meaningful if the prompt was answered
    if ST["fight_answered"] and post_s:
        vojas = bf_name_contains(post_s, 0, "Voja")
        tgt = post_s["objects"].get(str(ST["fight_target_oid"]), {})
        voja_dmg = max([damage_marked(post_s, oid) for oid, _ in vojas]
                       + [0])
        tgt_dmg = damage_marked(post_s, ST["fight_target_oid"])
        tgt_zone = tgt.get("zone")
        # Voja 3/3 vs Bear 2/2: bear takes 3 (dies), Voja takes 2
        fought = (tgt_dmg >= 3 or tgt_zone == "Graveyard") and voja_dmg >= 2
        A["A5_fight_resolved"] = "passed" if fought else "failed"
        D["A5_fight_resolved_detail"] = (
            f"voja_damage_marked={voja_dmg} target_damage_marked={tgt_dmg} "
            f"target_zone={tgt_zone}")
    elif ST["fight_prompted"]:
        A["A5_fight_resolved"] = "not-run"
        D["A5_fight_resolved_detail"] = "prompt seen but not answered"
    else:
        A["A5_fight_resolved"] = "not-run"
        D["A5_fight_resolved_detail"] = "no fight prompt; nothing to resolve"

    stack_empty = (not stack_entries(post_s)) if post_s else False
    A["A6_cleanup"] = "passed" if (stack_empty
                                   and ST["wolf_trigger_resolved"]) else (
        "failed" if post_s else "not-run")
    D["A6_cleanup_detail"] = (f"stack_empty={stack_empty} "
                              f"trigger_resolved={ST['wolf_trigger_resolved']}")

    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif A["A4_fight_prompted"] == "failed" and A["A2_wolf_trigger"] == \
            "passed" and A["A3_life_gain"] == "passed":
        verdict = "reproduced"
    elif A["A4_fight_prompted"] == "passed" and \
            A["A5_fight_resolved"] == "passed" and \
            A["A6_cleanup"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "waiting_for_seq": [w[0] for w in WF_SEEN],
                  "rejections": ST["rejections"]}
    json.dump(assertions, open(f"{EVDIR}/assertions.json", "w"), indent=1)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("final", assertions)
    await C0.close()
    await C1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
