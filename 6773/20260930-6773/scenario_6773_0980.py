#!/usr/bin/env python3
"""Issue #6773 revalidation on v0.98.0 (protocol 94): The Masamune does not
double death triggers (Toxrill slugs).

Reported (Discord): "[[The Masamune]] does not double death triggers, at
least not with [[Toxrill]] making slugs."

Oracle (pinned v0.98.0 card-data.json):
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

Pinned v0.98.0 parse: Masamune's third static ability is still a Continuous
grant to the equipped creature of DoubleTriggers { cause: CreatureDying }
(same as v0.79.0), so this is a runtime defect, not a parser gap.

Behavioral contract (single game, two human-client seats, v0.98.0/proto 94):
  RAMP   - P0 land-drops every tick (Island/Swamp balanced), keeps mulligan,
           casts The Masamune when affordable, then Toxrill when affordable.
  EQUIP  - P0 activates Masamune's Equip via the advertised ActivateAbility
           action (source_id/ability_index), falling back to the
           viewer_interaction exactChoices 'activateAbility' choice; then
           answers the TargetSelection schema prompt with Toxrill's candidate.
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

Protocol-94 driver notes (v0.98.0, 2026-09-30): HELLO advertises protocol 94
(exact match enforced); MulliganDecision as {"choice":{"type":"Keep"}} gated
on waiting_for.data.pending[] Declare; DiscardToHandSize via single SelectCards
{"cards":[...]} (revision-guarded); ActivateAbility via advertised action
(source_id/ability_index) as-is; TargetSelection via advertised schema
(sequence/select) with candidate references; PassPriority gated on the seat
genuinely holding priority; per-game guard reset; stale-client watchdog.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: E402
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6773")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260930-6773"
EVDIR = f"{BACKFILL}/evidence/6773/{EVID_RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
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


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.98.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

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
_DISCARD_REV = {}
_PASSED_REV = {}
_WATCH = {}


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand_ids(state, pid):
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
    for oid in hand_ids(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def obj_name(state, oid):
    o = objs(state).get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def pending_for(state, pid):
    data = wf_of(state).get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


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
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain", "Main"))


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and str((wf.get("data") or {}).get("player")) == str(pid)


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


async def do_mulligan(c, pid, tag, mulls):
    """Protocol-94 MulliganDecision {"choice":{"type":"Keep"}}, gated on the
    seat's presence in waiting_for.data.pending with phase Declare."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in mulls:
        return False
    say(f"[{tag}] mulligan keep")
    await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": "Keep"}}})
    mulls[tag] = True
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_discard(c, pid, tag, keep_names):
    """Protocol-94 DiscardToHandSize: single SelectCards with data.cards,
    revision-guarded against double submit."""
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    oids = hand_ids(state, pid)
    n = len(oids) - 7
    if n <= 0:
        return False

    def rank(oid):
        nm = lname(objs(state)[oid])
        if nm in keep_names:
            return 0
        return 2

    seen = set()
    ranked = []
    for oid in sorted(oids, key=rank):
        nm = lname(objs(state)[oid])
        if nm in keep_names and nm not in seen:
            seen.add(nm)
            continue
        ranked.append(oid)
    picks = [int(x) for x in ranked[:n]]
    if not picks:
        return False
    say(f"[{tag}] discarding to hand size: {[obj_name(state, x) for x in picks]} via SelectCards")
    wire("discard_action", {"who": tag, "type": "SelectCards", "data": {"cards": picks}})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pass_priority(c, pid):
    """Pass only if the seat genuinely holds priority (waiting_for Priority
    naming it); revision-aware to avoid double submit."""
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    state = st["state"]
    if not my_priority(state, pid):
        return False
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[c.name] = rev
            return True
    return False


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} wf={json.dumps(wf_of(s))[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


async def common_prefix(c, pid, tag, mulls, keep_names):
    """Mulligan/discard/payment/triggers/order handling shared by both seats.
    Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", [])
    if await do_mulligan(c, pid, tag, mulls):
        return True
    if await do_discard(c, pid, tag, keep_names):
        return True
    wf = wf_of(state)
    wtype = wf.get("type")
    wplayer = wf_player(state)
    if wtype == "ChooseLegend" and str(wplayer) == str(pid):
        ca = find_action(acts, "ChooseLegend")
        if ca:
            wire("action_submit", {"who": tag, "action": "ChooseLegend/asis"})
            await c.send_action(ca)
            say(f"[{tag}] legend rule: keeps first")
            return True
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": tag, "action": a["type"]})
            await c.send_action(a)
            return True
    if wtype == "OrderTriggers" and str(wplayer) == str(pid):
        oa = find_action(acts, "OrderTriggers")
        if oa:
            wire("action_submit", {"who": tag, "action": "OrderTriggers/asis"})
            await c.send_action(oa)
            return True
    return False


def decision_pending_for(state, pid):
    """True when the seat has a decision prompt that must be answered before
    any priority pass (equip choice, target selection, optional prompts)."""
    wf = wf_of(state)
    wtype = wf.get("type")
    if wtype in ("TargetSelection", "OptionalEffectChoice", "OptionalCostChoice",
                 "ManaPayment", "ChooseXValue", "SurveilChoice", "ScryChoice",
                 "ChooseManaColor", "ReplacementChoice"):
        return str(wf_player(state)) == str(pid)
    return False


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid, mulls):
    st = c.latest
    if not st:
        return False
    if await common_prefix(c, pid, "P0", mulls, {TOXRILL.lower(), MASAMUNE.lower()}):
        return True
    st = c.latest
    state, acts = st["state"], st.get("legal_actions", [])

    # Equip: first try the advertised ActivateAbility action (source_id match)
    mas = bf_named(state, 0, MASAMUNE)
    tox = bf_named(state, 0, TOXRILL)
    if mas and tox and not masamune_equipped_toxrill(state) and is_my_main(state, pid):
        act = next((a for a in acts
                    if a.get("type") == "ActivateAbility"
                    and str((a.get("data") or {}).get("source_id")) == str(mas[0])), None)
        if act is not None:
            say(f"[P0] equip via advertised ActivateAbility (source {mas[0]})")
            wire("equip_activate_action", {"action": act})
            await c.send_action(act)
            ST["equip_choice_submitted"] = True
            return True
        # fall back to the viewer_interaction exactChoices path
        if not ST["equip_choice_submitted"]:
            f = find_vi_choice(st, "activateAbility", source_ref=mas[0])
            if f:
                iid, ch = f
                wire("equip_choice_submit", {"iid": iid, "choiceId": ch.get("id")})
                say(f"[P0] equip choice submitted via viewer_interaction (masamune {mas[0]})")
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose", "data": {"choiceId": ch.get("id")}}} )
                ST["equip_choice_submitted"] = True
                return True

    # Target selection for the equip: answer with Toxrill's candidate
    if wf_of(state).get("type") == "TargetSelection" and str(wf_player(state)) == str(pid):
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
                 {"iid": iid, "refs": [str(cand_ref(ch)) for ch in chs]})
        wire("hold_priority", {"wtype": "TargetSelection"})
        return False

    # never pass priority while a P0 decision is pending
    if decision_pending_for(state, pid):
        wire("hold_priority", {"wtype": wf_of(state).get("type")})
        return False

    # empty combat declarations
    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        sub = json.loads(json.dumps(da))
        if isinstance(sub.get("data"), dict):
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub.get("data", {})})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = json.loads(json.dumps(db))
        if isinstance(sub.get("data"), dict):
            sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub.get("data", {})})
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
        # cast Masamune first (only if none on BF - legend rule), then Toxrill
        have_masamune = bool(bf_named(state, pid, MASAMUNE))
        have_toxrill = bool(bf_named(state, pid, TOXRILL))
        for nm in (MASAMUNE, TOXRILL):
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
                    say(f"[P0] casts {nm}")
                    return True

    return await pass_priority(c, pid)


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid, mulls):
    st = c.latest
    if not st:
        return False
    if await common_prefix(c, pid, "P1", mulls, {ELVES.lower()}):
        return True
    st = c.latest
    state, acts = st["state"], st.get("legal_actions", [])

    da = find_action(acts, "DeclareAttackers")
    if da:
        sub = json.loads(json.dumps(da))
        if isinstance(sub.get("data"), dict):
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub.get("data", {})})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = json.loads(json.dumps(db))
        if isinstance(sub.get("data"), dict):
            sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub.get("data", {})})
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
    if decision_pending_for(state, pid):
        wire("p1_hold", {"wtype": wf_of(state).get("type")})
        return False
    return await pass_priority(c, pid)


# ------------------------------------------------------------------ main

def stack_entries(state):
    return state.get("stack") or []


def _ability_description(entry):
    try:
        return entry["kind"]["data"]["ability"]["description"] or ""
    except (KeyError, TypeError):
        return ""


def is_death_trigger(entry):
    """True iff this stack entry is Toxrill's slime-DEATH trigger (not the
    end-step counter trigger). Discriminates on the trigger's own ability
    description: every Toxrill trigger entry embeds the source card's full
    data, so a bare "slug" substring match false-positives on the counter
    trigger."""
    try:
        if entry["kind"]["type"] != "TriggeredAbility":
            return False
    except (KeyError, TypeError):
        return False
    return "slime counter on it dies" in _ability_description(entry).lower()


def stack_death_trigger_count(state):
    return sum(1 for e in stack_entries(state) if is_death_trigger(e))


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]
    mulls = {}

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
        WIRE.close(); RUNLOG.close()
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
    TIMEOUT = 1800
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        watch(p0)
        watch(p1)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid, mulls):
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
                          "death_triggers": stack_death_trigger_count(state),
                          "entries": [json.dumps(e, default=str)[:400]
                                      for e in stack]})

        # MID: first revision with Toxrill's slime-DEATH trigger on the
        # stack (not the end-step counter trigger).
        if ST["pre_exported"] and not ST["mid_exported"]:
            n_death = stack_death_trigger_count(state)
            if n_death >= 1:
                mid = await do_export("mid_triggers.json")
                ST["mid_exported"] = True
                ST["mid_turn"] = mid.get("turn_number")
                ST["mid_slug_count"] = stack_death_trigger_count(mid)
                ST["mid_stack_size"] = len(stack_entries(mid))
                wire("mid_triggers", {"turn": ST["mid_turn"],
                                     "death_triggers": ST["mid_slug_count"],
                                     "stack_size": ST["mid_stack_size"]})
                say(f"[mid] exported: {ST['mid_slug_count']} slime-death "
                    f"triggers on stack")

        # POST: stack empty and the turn advanced past the mid turn's end step
        if ST["mid_exported"] and not ST["post_exported"]:
            if not stack_entries(state) and \
                    turn is not None and ST["mid_turn"] is not None \
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
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_death.json")
    mid = env_state("mid_triggers.json")
    post = env_state("post_resolution.json")

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
        ok_tox = bool(bf_named(pre, 0, TOXRILL))
        ok_equip = masamune_equipped_toxrill(pre)
        elv = bf_named(pre, 1, ELVES)
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
        n_trig = stack_death_trigger_count(mid)
        obs["notes"].append(f"post: elves_in_p1_gy={elves_gy}; mid: "
                            f"toxrill slug triggers on stack={n_trig}")
        A["A2_death_observed"] = "passed" if (elves_gy and n_trig >= 1) \
            else "failed"

    # A3
    if mid is None:
        A["A3_triggers_doubled"] = "not-run"
        obs["notes"].append("mid_triggers.json missing; A3 not-run")
    else:
        n = stack_death_trigger_count(mid)
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

    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        a3 = A.get("A3_triggers_doubled")
        a4 = A.get("A4_slugs_created")
        if a3 == "failed" or a4 == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"

    dur = time.time() - t0
    run = {
        "issue": 6773,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Toxrill, the Corrosive", 12], ["The Masamune", 8],
                   ["Island", 20], ["Swamp", 20]],
            "P1": [["Llanowar Elves", 12], ["Forest", 48]],
        },
        "assertions": A,
        "notes": obs["notes"] + [
            "protocol-94 driver (v0.98.0): mulligan as {choice:{type:Keep}} "
            "gated on pending[] Declare; DiscardToHandSize via single "
            "SelectCards(data.cards); equip via advertised ActivateAbility "
            "action (source_id/ability_index), viewer_interaction choice as "
            "fallback; TargetSelection via advertised schema; "
            "priority-gated passes; per-game guard reset; stale-client "
            "watchdog."
        ],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Toxrill / 8x Masamune deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "Emblem-trigger doubling half of the clause not tested "
            "(no emblems involved).",
            "States are authoritative exports, restorable only via full game "
            "replay (scenario_6773_0980.py), not direct load.",
        ],
        "setup_line": "P0: 12x Toxrill, the Corrosive + 8x The Masamune + 20x Island + 20x Swamp; "
                      "P1: 12x Llanowar Elves + 48x Forest (casts one Elves after equip)",
        "contract_line": ("Accept path: with Masamune equipped to Toxrill, a slime-counter "
                          "death must put 2 triggers on the stack -> 2 Slug tokens"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("verdict:", verdict)

    await p0.close()
    await p1.close()
    WIRE.close(); RUNLOG.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
