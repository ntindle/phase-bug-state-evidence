#!/usr/bin/env python3
"""Issue #6773: The Masamune does not double death triggers (Toxrill slugs).

Reported (Discord): "[[The Masamune]] does not double death triggers, at
least not with [[Toxrill]] making slugs."

Oracle (pinned v0.79.0 card-data.json):
  The Masamune ({3}, Legendary Artifact - Equipment):
    "As long as equipped creature is attacking, it has first strike and
     must be blocked if able.
     Equipped creature has "If a creature dying causes a triggered ability
     of this creature or an emblem you own to trigger, that ability triggers
     an additional time."
     Equip {2}"
  Toxrill, the Corrosive ({5}{B}{B}, 7/7):
    "At the beginning of each end step, put a slime counter on each
     creature you don't control.
     Creatures you don't control get -1/-1 for each slime counter on them.
     Whenever a creature you don't control with a slime counter on it dies,
     create a 1/1 black Slug creature token."

Pinned parse: Masamune's third static ability is a Continuous grant to the
equipped creature of DoubleTriggers { cause: CreatureDying } (NOT the
generic grant the triage analysis described), so the parser side looks
supported on v0.79.0; this run tests the runtime behavior.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  RAMP   - P0 land-drops every tick (Island/Swamp balanced), keeps mulligan,
           casts The Masamune when affordable, then Toxrill when affordable.
  EQUIP  - P0 activates Masamune's Equip via the viewer_interaction
           exactChoices 'activateAbility' choice, then answers the
           TargetSelection schema prompt with Toxrill's candidate.
  VICTIM - P1 casts exactly one Llanowar Elves, gated on the equip being
           complete, and holds priority passes otherwise.
  DEATH  - at the next end step, Toxrill's counter trigger puts a slime
           counter on the 1/1 Elves; it dies as a 0/0 SBA; Toxrill's
           death trigger fires and, per the granted DoubleTriggers, should
           trigger an additional time -> two triggers on the stack.
  PRE    - exported once: Toxrill equipped, Elves on P1 BF, 0 slime counters.
  MID    - exported at the first revision where a Toxrill-sourced trigger
           sits on the stack; slug-trigger entries counted (expect 2).
  POST   - exported after the stack empties past the end step; Slug tokens
           counted (expect 2).

  A1 setup_ok         pre: Toxrill on P0 BF, Masamune attached to Toxrill,
                      Elves on P1 BF with no slime counters.
  A2 death_observed   post: Elves in P1 graveyard; mid shows >=1
                      Toxrill-sourced trigger on the stack.
  A3 triggers_doubled  mid: exactly 2 Toxrill slug-trigger stack entries.
  A4 slugs_created     post: exactly 2 Slug tokens on P0 BF.
  A5 cleanup           post: stack empty.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1+A2 pass and A3/A4 show 1 instead of 2.
Verdict = not-reproduced iff A1..A5 all pass.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as client_mod  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client_mod.URL = "ws://127.0.0.1:9375/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6773"
EVID_ISSUE = "6773"
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


MASAMUNE = "The Masamune"
TOXRILL = "Toxrill, the Corrosive"
ELVES = "Llanowar Elves"
ISLAND = "Island"
SWAMP = "Swamp"
FOREST = "Forest"

P0_DECK = deck((TOXRILL, 12), (MASAMUNE, 8), (ISLAND, 20), (SWAMP, 20))
P1_DECK = deck((ELVES, 12), (FOREST, 48))

ST = {"equip_choice_submitted": False, "pre_exported": False,
      "mid_exported": False, "mid_turn": None, "post_exported": False,
      "stop": False, "equip_done": False, "cast_masamune": False,
      "cast_toxrill": False}
WF_SEEN = []
SUBMITTED_IID = set()


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == name.lower()]


def gy_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and lname(o) == name.lower()]


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def ints_in(x):
    out = []
    if isinstance(x, bool):
        return out
    if isinstance(x, int):
        return [x]
    if isinstance(x, dict):
        for v in x.values():
            out.extend(ints_in(v))
    elif isinstance(x, (list, tuple)):
        for v in x:
            out.extend(ints_in(v))
    return out


def attached_to_oid(obj):
    at = obj.get("attached_to")
    if at is None:
        return None
    ints = ints_in(at)
    return ints[0] if ints else None


def masamune_equipped_toxrill(state):
    tox = bf_named(state, 0, TOXRILL)
    if not tox:
        return False
    tox_oid = str(tox[0])
    for oid in bf_named(state, 0, MASAMUNE):
        if str(attached_to_oid(objs(state)[oid])) == tox_oid:
            return True
    return False


def slime_count(obj):
    c = obj.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            if "slime" in str(k).lower():
                try:
                    return int(v)
                except Exception:
                    return 1
        return 0
    if isinstance(c, list):
        n = 0
        for e in c:
            if "slime" in json.dumps(e).lower():
                n += 1
        return n
    return 0


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def vi_opps(st):
    return ((st.get("viewer_interaction") or {}).get("opportunities", [])) or []


def find_vi_choice(st, code, source_ref=None):
    for op in vi_opps(st):
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []) or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [s.get("data", {}).get("reference")
                        for s in ch.get("surfaces", []) or []
                        if s.get("data", {}).get("role") == "source"]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
    return None


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def find_target_opportunity(st):
    for op in vi_opps(st):
        if op.get("interactionId") in SUBMITTED_IID:
            continue
        resp = op.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if spec_type not in ("sequence", "select"):
            continue
        chs = data.get("choices") or data.get("candidates") or []
        if any(cand_ref(ch) is not None for ch in chs):
            return op.get("interactionId"), spec_type, chs
    return None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)

        def rank(oid):
            nm = lname(objs(state)[oid])
            if nm == TOXRILL.lower():
                return 0
            if nm == MASAMUNE.lower():
                return 1
            return 2
        # keep one copy of each spell; dump extras first
        seen = set()
        ranked = []
        for oid in sorted(oids, key=rank):
            nm = lname(objs(state)[oid])
            if nm in (TOXRILL.lower(), MASAMUNE.lower()) and nm not in seen:
                seen.add(nm)
                continue
            ranked.append(oid)
        picks = ranked[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                  "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)} to hand size")
            return True
        return False

    if wtype == "ChooseLegend" and wplayer == 0:
        ca = find_action(acts, "ChooseLegend")
        if ca:
            wire("action_submit", {"who": "P0", "action": "ChooseLegend/asis"})
            await c.send_action(ca)
            say("[P0] legend rule: keeps first")
            return True
        return False

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    if wtype == "OrderTriggers" and wplayer == 0:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            wire("action_submit", {"who": "P0", "action": "OrderTriggers/asis"})
            await c.send_action(oa)
            return True

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "OptionalEffectChoice", "TargetSelection",
                 "ManaPayment", "ChooseXValue", "SurveilChoice",
                 "ScryChoice") and wplayer == 0:
        if wtype == "TargetSelection":
            tox = bf_named(state, 0, TOXRILL)
            found = find_target_opportunity(st)
            if found and tox:
                iid, spec_type, chs = found
                pick = None
                for ch in chs:
                    if str(cand_ref(ch)) == str(tox[0]):
                        pick = ch
                        break
                if pick is not None:
                    sub = {"interactionId": iid,
                           "response": {"type": spec_type,
                                        "data": {"choiceIds": [pick["id"]]}}}
                    wire("equip_target_submit", sub)
                    say(f"[P0] equip target -> Toxrill (candidate {pick['id']})")
                    await c.send_interaction(sub)
                    SUBMITTED_IID.add(iid)
                    ST["equip_done"] = True
                    return True
                wire("equip_target_deferred",
                     {"iid": iid,
                      "refs": [str(cand_ref(ch)) for ch in chs]})
                return False
        wire("hold_priority", {"wtype": wtype})
        return False

    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    if is_my_main(state, pid):
        # land drop every tick: color-balanced
        n_isle = len(bf_named(state, pid, ISLAND))
        n_swamp = len(bf_named(state, pid, SWAMP))
        order = (ISLAND, SWAMP) if n_isle <= n_swamp else (SWAMP, ISLAND)
        for ln in order:
            hid = find_hand(state, pid, ln)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    wire("action_submit", {"who": "P0", "action": f"PlayLand/{ln}"})
                    await c.send_action(a)
                    return True
        # equip Masamune to Toxrill via activateAbility choice
        mas = bf_named(state, pid, MASAMUNE)
        tox = bf_named(state, pid, TOXRILL)
        if mas and tox and not masamune_equipped_toxrill(state) \
                and not ST["equip_choice_submitted"]:
            f = find_vi_choice(st, "activateAbility", source_ref=mas[0])
            if f:
                iid, ch = f
                wire("equip_choice_submit",
                     {"iid": iid, "choiceId": ch.get("id")})
                say(f"[P0] equip choice submitted (masamune {mas[0]})")
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose",
                                  "data": {"choiceId": ch.get("id")}}})
                ST["equip_choice_submitted"] = True
                return True
        # cast Masamune first (only if none on BF - legend rule), then Toxrill
        # (also only if none on BF - legend rule)
        have_masamune = bool(bf_named(state, pid, MASAMUNE))
        have_toxrill = bool(bf_named(state, pid, TOXRILL))
        for nm, flag in ((MASAMUNE, "cast_masamune"), (TOXRILL, "cast_toxrill")):
            if nm == MASAMUNE and have_masamune:
                continue
            if nm == TOXRILL and have_toxrill:
                continue
            oid = find_hand(state, pid, nm)
            for a in acts:
                if a["type"] == "CastSpell" and oid \
                        and str(a.get("data", {}).get("object_id")) == str(oid):
                    wire("action_submit", {"who": "P0", "action": f"CastSpell/{nm}",
                                          "object_id": oid})
                    await c.send_action(a)
                    ST[flag] = True
                    say(f"[P0] casts {nm}")
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

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)
        seen = set()
        ranked = []
        for oid in oids:
            nm = lname(objs(state)[oid])
            if nm == ELVES.lower() and nm not in seen:
                seen.add(nm)
                continue
            ranked.append(oid)
        picks = ranked[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
    if wtype == "OrderTriggers" and wplayer == 1:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    da = find_action(acts, "DeclareAttackers")
    if da:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        hid = find_hand(state, pid, FOREST)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == str(hid):
                await c.send_action(a)
                return True
        # cast exactly one Elves, gated on the equip being complete
        if masamune_equipped_toxrill(state) and not ST["mid_exported"]:
            if not bf_named(state, pid, ELVES):
                oid = find_hand(state, pid, ELVES)
                for a in acts:
                    if a["type"] == "CastSpell" and oid \
                            and str(a.get("data", {}).get("object_id")) == str(oid):
                        await c.send_action(a)
                        say("[P1] casts Llanowar Elves")
                        return True
    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------------ main

def stack_entries(state):
    return state.get("stack") or []


def entry_mentions(entry, *words):
    low = json.dumps(entry, default=str).lower()
    return all(w in low for w in words)


def _ability_description(entry):
    try:
        return entry["kind"]["data"]["ability"]["description"] or ""
    except (KeyError, TypeError):
        return ""


def is_death_trigger(entry):
    """True iff this stack entry is Toxrill's slime-death trigger (not the
    end-step counter trigger). Discriminates on the trigger's own ability
    description: every Toxrill trigger entry embeds the source card's full
    data (subtypes ["slug","horror"], all three ability texts), so a bare
    "slug" substring match false-positives on the counter trigger."""
    try:
        if entry["kind"]["type"] != "TriggeredAbility":
            return False
    except (KeyError, TypeError):
        return False
    return "slime counter on it dies" in _ability_description(entry).lower()


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
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)

        turn = state.get("turn_number")
        phase = state.get("phase") or ""

        # PRE: Toxrill equipped, Elves on P1 BF, no slime counters yet
        if not ST["pre_exported"] and masamune_equipped_toxrill(state):
            elves = bf_named(state, 1, ELVES)
            if elves and slime_count(objs(state)[elves[0]]) == 0:
                pre = await do_export("pre_death.json")
                ST["pre_exported"] = True
                wire("pre_death", {"turn": pre.get("turn_number"),
                                  "phase": pre.get("phase"),
                                  "equipped": masamune_equipped_toxrill(pre),
                                  "elves_bf": len(bf_named(pre, 1, ELVES))})
                say(f"[pre] exported turn {pre.get('turn_number')} "
                    f"phase {pre.get('phase')}")

        # Stack audit: log every distinct stack snapshot from pre through
        # post so the trigger count is auditable even if an export lands
        # between resolutions.
        if ST["pre_exported"] and not ST["post_exported"]:
            stack = stack_entries(state)
            if stack:
                sig = json.dumps(stack, default=str, sort_keys=True)
                if sig != ST.get("last_stack_sig"):
                    ST["last_stack_sig"] = sig
                    wire("stack_snapshot",
                         {"turn": turn, "phase": phase,
                          "death_triggers": sum(1 for e in stack
                                                if is_death_trigger(e)),
                          "entries": [json.dumps(e, default=str)[:400]
                                      for e in stack]})

        # MID: first revision with Toxrill's slime-DEATH trigger on the
        # stack (not the end-step counter trigger).
        if ST["pre_exported"] and not ST["mid_exported"]:
            stack = stack_entries(state)
            n_death = sum(1 for e in stack if is_death_trigger(e))
            if n_death >= 1:
                mid = await do_export("mid_triggers.json")
                ST["mid_exported"] = True
                ST["mid_turn"] = mid.get("turn_number")
                ST["mid_slug_count"] = sum(
                    1 for e in stack_entries(mid) if is_death_trigger(e))
                ST["mid_stack_size"] = len(stack_entries(mid))
                wire("mid_triggers", {"turn": ST["mid_turn"],
                                     "death_triggers": ST["mid_slug_count"],
                                     "stack_size": ST["mid_stack_size"]})
                say(f"[mid] exported: {ST['mid_slug_count']} slime-death "
                    f"triggers on stack")

        # POST: stack empty and the game moved past the mid turn's end step
        if ST["mid_exported"] and not ST["post_exported"]:
            if not stack_entries(state) and \
                    (turn != ST["mid_turn"] or phase not in ("End",)):
                # require the turn to have actually advanced past mid
                if turn is not None and ST["mid_turn"] is not None \
                        and turn > ST["mid_turn"]:
                    await asyncio.sleep(1.0)
                    post = await do_export("post_resolution.json")
                    ST["post_exported"] = True
                    slugs = [oid for oid, o in objs(post).items()
                             if o.get("zone") == "Battlefield"
                             and o.get("controller") == 0
                             and "slug" in lname(o)]
                    wire("post_resolution", {"turn": post.get("turn_number"),
                                            "phase": post.get("phase"),
                                            "slugs": len(slugs),
                                            "stack_empty": True})
                    say(f"[post] exported: {len(slugs)} Slug tokens")
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

    pre = env_state("pre_death.json")
    mid = env_state("mid_triggers.json")
    post = env_state("post_resolution.json")

    def count_slug_triggers(s):
        return sum(1 for e in stack_entries(s) if is_death_trigger(e))

    def count_slugs(s):
        return sum(1 for o in objs(s).values()
                   if o.get("zone") == "Battlefield"
                   and o.get("controller") == 0
                   and "slug" in lname(o))

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_death.json never exported")
    else:
        tox = bf_named(pre, 0, TOXRILL)
        mas = bf_named(pre, 0, MASAMUNE)
        elv = bf_named(pre, 1, ELVES)
        ok_tox = bool(tox)
        ok_equip = masamune_equipped_toxrill(pre)
        ok_elves = bool(elv)
        ok_counters = ok_elves and slime_count(objs(pre)[elv[0]]) == 0
        obs["notes"].append(
            f"pre_death: turn={pre.get('turn_number')} phase={pre.get('phase')} "
            f"toxrill_bf={ok_tox} masamune_attached={ok_equip} "
            f"elves_bf={ok_elves} slime_counters_zero={ok_counters}")
        A["A1_setup_ok"] = "passed" if (ok_tox and ok_equip and ok_elves
                                       and ok_counters) else "failed"

    # A2
    if post is None or mid is None:
        A["A2_death_observed"] = "not-run"
        obs["notes"].append("mid or post missing; A2 not-run")
    else:
        elves_gy = bool(gy_named(post, 1, ELVES))
        n_trig = count_slug_triggers(mid)
        obs["notes"].append(f"post: elves_in_p1_gy={elves_gy}; mid: "
                            f"toxrill slug triggers on stack={n_trig}")
        A["A2_death_observed"] = "passed" if (elves_gy and n_trig >= 1) \
            else "failed"

    # A3
    if mid is None:
        A["A3_triggers_doubled"] = "not-run"
        obs["notes"].append("mid_triggers.json missing; A3 not-run")
    else:
        n = count_slug_triggers(mid)
        obs["notes"].append(f"mid: {n} Toxrill slug triggers on stack "
                            f"(expected 2 with doubling)")
        A["A3_triggers_doubled"] = "passed" if n == 2 else "failed"

    # A4
    if post is None:
        A["A4_slugs_created"] = "not-run"
        obs["notes"].append("post_resolution.json missing; A4 not-run")
    else:
        n = count_slugs(post)
        obs["notes"].append(f"post: {n} Slug tokens on P0 battlefield "
                            f"(expected 2 with doubling)")
        A["A4_slugs_created"] = "passed" if n == 2 else "failed"

    # A5
    if post is None:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append("post_resolution.json missing; A5 not-run")
    else:
        empty = not stack_entries(post)
        obs["notes"].append(f"post: stack_empty={empty} "
                            f"turn={post.get('turn_number')} "
                            f"phase={post.get('phase')}")
        A["A5_cleanup"] = "passed" if empty else "failed"

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        a3 = A.get("A3_triggers_doubled")
        a4 = A.get("A4_slugs_created")
        if a3 == "failed" or a4 == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6773,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Toxrill, the Corrosive": 12, "The Masamune": 8,
                   "Island": 20, "Swamp": 20},
            "P1": {"Llanowar Elves": 12, "Forest": 48},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6773.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
