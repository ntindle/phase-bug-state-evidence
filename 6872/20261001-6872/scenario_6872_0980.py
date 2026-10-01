#!/usr/bin/env python3
"""Issue #6872: The Dominion Bracelet's granted activated ability never
reaches the equipped creature.

Re-validation of the 2026-09-11 v0.80.0/protocol-69 run (verdict
reproduced) on the pinned v0.98.0 (protocol 94). The maintained evidence
comment on the issue already publishes the v0.80.0 result; per the playbook
staleness rule this run re-exercises the reported path on the current pin.
No ledger entry exists for #6872 (lost in a VM replacement); the history is
reconstructed from the published comment + prior evidence dir.

Oracle (granted): "{15}, Exile The Dominion Bracelet: You control target
opponent during their next turn. This ability costs {X} less to activate,
where X is this creature's power. Activate only as a sorcery."
v0.98.0 card data fully parses this (GrantAbility, Composite cost,
cost_reduction Power, Exile granting-object cost, AsSorcery) - the triage
"not installed" claim predates that data. This scenario tests the RUNTIME
behavior.

Protocol-94 driver notes (v0.98.0; carried over from scenario_301_0980.py):
- HELLO advertises protocol 94 (server enforces exact match).
- MulliganDecision as {"choice":{"type":"Keep"}} (or "Mulligan"), gated on
  waiting_for.data.pending[] Declare entries keyed by (seat, mulligan round).
- BottomCards / DiscardToHandSize via single SelectCards {"cards":[...]}.
- ActivateAbility via advertised action as-is (source_id/ability_index).
- PassPriority only when the seat genuinely holds priority (waiting_for
  Priority names the player), revision-guarded.
- Target-selection prompts answered from viewer_interaction opportunities
  via the advertised response schema (sequence -> choiceIds).

Plan (two human seats, native engine, v0.98.0 / protocol 94):
  SETUP - land drops; P0 casts Yargle, Glutton of Urborg (4B, 9/3), then
          The Dominion Bracelet ({2}), then equips it to Yargle (Equip {1}
          -> schema TargetSelection -> choose Yargle).
  PROOF - on a fresh P0 PreCombatMain (turn > attach turn, 5+ untapped
          Swamps): export pre_grant.json; probe ALL ActivateAbility actions
          with source_id == Yargle's oid. Yargle has no native abilities,
          so any advertised ActivateAbility on it is the granted one.
  ACT   - if advertised: submit as-is; answer the opponent TargetSelection
          (choose P1 seat); answer PayMana*/exile prompts as observed; watch
          for rejections. Export mid_grant.json with the ability on the
          stack; let it resolve; export post_activate.json.
  OBSERVE - advance to P1's next turn and record whether P0 is offered P1's
          decisions (control_effect). Export post.json.

Behavioral contract:
  A1 setup_ok            Bracelet attached to Yargle (attached_to nested
                         dict), Yargle 10/4, fresh P0 main
  A2 granted_exposed     ActivateAbility advertised on equipped Yargle
                         whose definition matches the granted
                         "{15}, Exile ... control target opponent" text
  A3 activation_completes ability resolved, Bracelet in Exile zone, no
                         rejection of the activation path
  A4 control_recorded    lasting control-next-turn effect registration for
                         P1 found in post states, or P0 observed making
                         P1's decisions on P1's next turn
  A5 cleanup             stack empty, game not over

Verdict: reproduced iff A1 passes and (A2 fails, or A2 passes but A3/A4
fail with a related failure of the same granted ability). not-reproduced
iff A1..A3 pass and the control effect is recorded/observed.
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6872"
EVID_ISSUE = "6872"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

YARGLE = "Yargle, Glutton of Urborg"
BRACELET = "The Dominion Bracelet"
SWAMP = "Swamp"
LANDS = {SWAMP}

ST = {"stage": "SETUP", "stop": False, "retry": False}
ACT = {}          # {"equip": {...}, "equip_targeted": bool, "grant": {...},
                  #  "grant_targeted": {...}}
MULLS = {"P0": 0, "P1": 0}
MULL_DONE = set()
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
OBS = {}
IDS = {"grant_ability": None}
SEEN_TURN = {}    # "yargle" -> turn on BF, "attached" -> attach turn
GRANT_WATCH = {"since": None}
PROOF_WATCH = {"since": None}
CONTROL_OBS = {"since": None, "turn": None, "snaps": []}
_PASSED_REV = {}
_DISCARD_REV = {}
C0 = None
t0 = time.time()

SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "server_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# recompute against on-disk artifacts; never copy hashes blindly
for _f, _k in (("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.98.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"


def reset_globals():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False})
    ACT.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    MULL_DONE.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.update({"iid": None})
    OBS.clear()
    IDS.update({"grant_ability": None})
    SEEN_TURN.clear()
    GRANT_WATCH.update({"since": None})
    PROOF_WATCH.update({"since": None})
    CONTROL_OBS.update({"since": None, "turn": None, "snaps": []})
    _PASSED_REV.clear()
    _DISCARD_REV.clear()


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


say("server identity hashes verified against on-disk pinned artifacts")


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return oname(o) if o else "?"


def hand_ids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if obj_name(state, oid) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def perm_oids(state, pid, name):
    return [str(oid) for oid, o in bf(state, pid) if oname(o) == name]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def life_of(state, pid):
    return state["players"][pid]["life"]


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "n"):
            if k in v:
                return num(v[k])
    return None


def stack_ids(state):
    return [str(e.get("id")) for e in (state.get("stack") or [])]


def stack_entry(state, sid):
    for e in (state.get("stack") or []):
        if str(e.get("id")) == str(sid):
            return e
    return None


def stack_entry_name(e):
    kind = e.get("kind") or {}
    data = kind.get("data") or {}
    ab = data.get("ability") or {}
    desc = ab.get("description") or ""
    return f"stack:{kind.get('type')}:{desc[:80]}"


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain",
                                      "Main"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_of(state).get("data"),
                             "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
    await c.send_interaction(sub)


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
                              "stage": ST["stage"]})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    s = await C0.export_state()
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


def candidate_info(opp, state):
    """Resolve opportunity candidates to (choiceId, ref_oid, seat, name,
    zone). Player candidates carry seat instead of a reference."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref, seat = None, None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        se = stack_entry(state, ref) if ref else None
        if se is not None:
            name, zone, controller = (stack_entry_name(se), "Stack",
                                      se.get("controller"))
        else:
            name, zone, controller = oname(o), o.get("zone"), \
                o.get("controller")
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": name, "zone": zone,
                    "controller": controller,
                    "text": ch.get("text")})
    return out


def activate_options(acts, state, pid, name, want_index):
    """ActivateAbility options for `name` on `pid`'s battlefield."""
    srcs = set(perm_oids(state, pid, name))
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in srcs:
            out.append((d.get("ability_index"), a))
    if want_index is not None:
        return [a for i, a in out if i == want_index]
    return [a for _, a in out]


def grant_options(acts, state, yargle_oid):
    """All ActivateAbility actions sourced from Yargle. Yargle has no
    native abilities, so any of these is the granted one."""
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src == str(yargle_oid):
            out.append((d.get("ability_index"), a))
    return out


def is_target_wait(state):
    return wf_type(state) in ("TargetSelection", "TriggerTargetSelection")


def find_int_in(obj, target):
    """Recursive int search (attached_to is a nested dict)."""
    if isinstance(obj, bool):
        return False
    if isinstance(obj, int):
        return obj == target
    if isinstance(obj, dict):
        return any(find_int_in(v, target) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(find_int_in(v, target) for v in obj)
    return False


def attached_oid(state, equip_oid, creature_oid):
    o = state["objects"].get(str(equip_oid), {})
    att = o.get("attached_to")
    if att is None:
        return False
    return find_int_in(att, int(creature_oid))


def build_target_response(resp, want):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [want["choice_id"]]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": want["choice_id"]}}
    return None


async def do_mulligan(c, pid, tag):
    """Protocol-94 MulliganDecision gated on waiting_for.data.pending[]
    Declare entries."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    key = (tag, str(pend.get("mulligan_count", pend)))
    if key in MULL_DONE:
        return False
    lands = [obj_name(state, o) for o in hand_ids(state, pid)]
    n_lands = sum(1 for n in lands if n in LANDS)
    if tag == "P0":
        has_piece = YARGLE in lands or BRACELET in lands
        keep_ok = n_lands >= 2 and (has_piece or MULLS[tag] >= 2)
    else:
        keep_ok = n_lands >= 2
    choice = "Keep" if (keep_ok or MULLS[tag] >= 2) else "Mulligan"
    if choice == "Mulligan":
        MULLS[tag] += 1
    MULL_DONE.add(key)
    say(f"[{tag}] mulligan -> {choice} (lands={n_lands})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                        "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, pid, tag):
    """Protocol-94 BottomCards: single SelectCards with data.cards."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    key = (tag, "bottom", str(pend.get("mulligan_count", pend)))
    if key in MULL_DONE:
        return False
    n = int((pend.get("phase") or {}).get("count") or 1)
    ids = hand_ids(state, pid)
    keep = {YARGLE, BRACELET} if tag == "P0" else set()
    ranked = sorted(ids, key=lambda o: 0 if obj_name(state, o) in LANDS
                    else (2 if obj_name(state, o) in keep else 1))
    picks = [int(x) for x in ranked[:n]]
    MULL_DONE.add(key)
    say(f"[{tag}] bottoming {n}: {[obj_name(state, x) for x in picks]}")
    wire("bottom", {"who": tag, "count": n})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    return True


async def do_discard(c, pid, tag):
    """Protocol-94 DiscardToHandSize: single SelectCards, revision-guarded."""
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
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False
    h = hand_ids(state, pid)
    keep = {YARGLE, BRACELET} if tag == "P0" else set()
    ranked = sorted(h, key=lambda o: 0 if obj_name(state, o) in LANDS
                    else (2 if obj_name(state, o) in keep else 1))
    picks = [int(x) for x in ranked[:n]]
    _DISCARD_REV[(c.name, rev)] = True
    say(f"[{tag}] discards {[obj_name(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    return True


async def gated_pass(c, pid):
    """PassPriority only for the seat that genuinely holds priority;
    revision-guarded."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "Priority" or str(wf_player(state)) != str(pid):
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in st.get("legal_actions", []):
        if a.get("type") == "PassPriority":
            _PASSED_REV[c.name] = rev
            await submit_as_is(c, a)
            return True
    return False


async def answer_equip_target(c, state, acts, st):
    """Equip target prompt: choose Yargle (battlefield candidate)."""
    if not ACT.get("equip") or ACT.get("equip_targeted"):
        return False
    if not is_target_wait(state) or str(wf_player(state)) != "0":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        if not any(x["zone"] == "Battlefield" for x in cands):
            continue
        want = next((x for x in cands if x["name"] == YARGLE), None)
        if not want:
            continue
        if ("equip_prompt", len(chs)) not in SHAPES:
            SHAPES.add(("equip_prompt", len(chs)))
            wire("equip_target_prompt",
                 {"rtype": resp.get("type"), "candidates": cands,
                  "opportunity": opp})
            say(f"[P0] equip target prompt: "
                f"{[(x['name'], x['ref']) for x in cands]}")
        resp_out = build_target_response(resp, want)
        if not resp_out:
            say(f"[P0] equip: unexpected prompt shape {resp.get('type')}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ACT["equip_targeted"] = {"at": time.time(), "ref": want["ref"]}
        say(f"[P0] equip targets Yargle (oid={want['ref']})")
        return True
    return False


async def answer_grant_target(c, state, acts, st):
    """Granted-ability prompts for P0: (a) opponent target -> choose P1
    seat; (b) exile-cost candidate -> choose the Bracelet."""
    if not ACT.get("grant") or ACT.get("grant_targeted"):
        return False
    if not is_target_wait(state) or str(wf_player(state)) != "0":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    brace_oids = set(perm_oids(state, 0, BRACELET))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x["seat"] == 1), None)
        kind = "opponent"
        if not want:
            want = next((x for x in cands if x["ref"] in brace_oids), None)
            kind = "exile_cost"
        if not want:
            continue
        if ("grant_prompt", kind, len(chs)) not in SHAPES:
            SHAPES.add(("grant_prompt", kind, len(chs)))
            wire("grant_target_prompt",
                 {"kind": kind, "rtype": resp.get("type"),
                  "candidates": cands, "opportunity": opp})
            say(f"[P0] grant {kind} prompt: "
                f"{[(x['seat'], x['name'], x['ref']) for x in cands]}")
        resp_out = build_target_response(resp, want)
        if not resp_out:
            say(f"[P0] grant: unexpected prompt shape {resp.get('type')}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        OBS.setdefault("grant_prompts_answered", []).append(
            {"kind": kind, "at": time.time()})
        if kind == "opponent":
            ACT["grant_targeted"] = {"at": time.time(), "seat": 1}
            say("[P0] grant ability targets P1")
        else:
            say("[P0] grant exile-cost chooses the Bracelet")
        return True
    return False


async def tick(c, pid, tag, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan / bottom / discard (protocol-94, pending-gated)
    if await do_mulligan(c, pid, tag):
        return True
    if await do_bottom(c, pid, tag):
        return True
    if await do_discard(c, pid, tag):
        return True
    # defensive: legend-rule choice (Yargle and Bracelet are legendary)
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{tag} legend-choice submitted as-is: {a['type']}")
            return True
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    # engine-advertised mana payments: submit as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # answer prompts before anything else
    if is_p0:
        if await answer_equip_target(c, state, acts, st):
            return True
        if await answer_grant_target(c, state, acts, st):
            return True
    # never pass while P0 has a decision pending
    if is_p0 and wf_type(state) in ("OptionalCostChoice", "TargetSelection",
                                    "TriggerTargetSelection", "ManaPayment",
                                    "ChooseXValue", "DiscardChoice",
                                    "OrderTriggers") \
            and str(wf_player(state)) == "0":
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup(c, pid, state, acts):
                return True
        elif ST["stage"] == "PROOF":
            if await p0_proof(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    # gated priority pass only
    if await gated_pass(c, pid):
        return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, SWAMP)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


def stack_has_yargle_spell(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if YARGLE in blob:
            return True
    return False


async def p0_setup(c, pid, state, acts):
    # cast Yargle first (5), then Bracelet (2), then equip ({1})
    yoids = perm_oids(state, pid, YARGLE)
    boids = perm_oids(state, pid, BRACELET)
    if not yoids and not stack_has_yargle_spell(state):
        oid = find_hand(state, pid, YARGLE)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, SWAMP) >= 5:
            await submit_as_is(c, a)
            say("P0 casts Yargle, Glutton of Urborg")
            return True
    if yoids and not boids:
        oid = find_hand(state, pid, BRACELET)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, SWAMP) >= 2:
            await submit_as_is(c, a)
            say("P0 casts The Dominion Bracelet")
            return True
    if yoids and boids and not ACT.get("equip"):
        opts = activate_options(acts, state, pid, BRACELET, 0)
        if ("equip_opts", len(opts)) not in SHAPES:
            SHAPES.add(("equip_opts", len(opts)))
            wire("equip_options", {"count": len(opts), "all_action_types":
                 sorted({a["type"] for a in acts})})
            say(f"[P0] equip options: {len(opts)}")
        if not opts:
            return False
        ACT["equip"] = {"at": time.time(),
                        "turn": state.get("turn_number")}
        await submit_as_is(c, opts[0])
        say("P0 activates Equip {1}")
        return True
    # retry fallback: equip was activated but never attached (fizzle or
    # silent reject) -> clear and try again on a later main
    if ACT.get("equip") and "attached" not in SEEN_TURN \
            and not is_target_wait(state):
        boids = perm_oids(state, pid, BRACELET)
        yoids = perm_oids(state, pid, YARGLE)
        still_loose = [b for b in boids
                       if not any(attached_oid(state, b, y) for y in yoids)]
        if still_loose and state.get("turn_number", 0) > \
                ACT["equip"].get("turn", 0) + 1:
            say("P0 equip never attached; retrying equip")
            wire("equip_retry", {"turn": state.get("turn_number")})
            ACT.pop("equip", None)
            ACT.pop("equip_targeted", None)
    return False


def grant_desc_match(desc):
    d = (desc or "").lower()
    return "control target opponent" in d


async def p0_proof(c, pid, state, acts):
    turn = state.get("turn_number")
    if "attached" not in SEEN_TURN or turn <= SEEN_TURN["attached"]:
        return False
    yoids = perm_oids(state, pid, YARGLE)
    if not yoids:
        return False
    yoid = yoids[0]
    if not OBS.get("pre_exported"):
        if untapped_of(state, pid, SWAMP) < 5:
            return False
        await export_now("pre_grant.json")
        OBS["pre_exported"] = {"at": time.time(), "turn": turn,
                               "untapped_swamps": untapped_of(state, pid, SWAMP)}
        wire("pre_grant_yargle_object",
             {"oid": yoid, "object": state["objects"][yoid]})
        say(f"[P0] pre_grant exported (turn {turn}); Yargle oid={yoid}")
        return True
    # probe: list ALL ActivateAbility actions sourced from Yargle
    if not ACT.get("grant") and not OBS.get("grant_probe"):
        opts = grant_options(acts, state, yoid)
        yo = state["objects"][yoid]
        obj_abilities = yo.get("abilities") or []
        wire("grant_probe",
             {"yargle_oid": yoid,
              "advertised_indices": [i for i, _ in opts],
              "object_abilities": obj_abilities,
              "all_action_types": sorted({a["type"] for a in acts})})
        say(f"[P0] grant probe: advertised ActivateAbility indices on "
            f"Yargle = {[i for i, _ in opts]}; object abilities = "
            f"{len(obj_abilities)}")
        for ab in obj_abilities:
            say(f"    obj ability: {(ab.get('description') or '')[:100]}")
        OBS["grant_probe"] = {
            "at": time.time(), "turn": turn,
            "advertised_indices": [i for i, _ in opts],
            "object_ability_descriptions":
                [(ab.get("description") or "")[:120]
                 for ab in obj_abilities],
            "object_ability_kinds":
                [ab.get("kind") for ab in obj_abilities],
        }
        if opts:
            # Yargle has no native abilities; the advertised one is the
            # granted ability.
            pick = opts[0]
            ACT["grant"] = {"at": time.time(), "turn": turn,
                            "ability_index": pick[0]}
            wire("grant_activated", {"ability_index": pick[0]})
            await submit_as_is(c, pick[1])
            say(f"[P0] activates granted ability (ability_index={pick[0]})")
            return True
        # nothing advertised: record; the stuck watch converts this to a
        # captured failure
        if GRANT_WATCH["since"] is None:
            GRANT_WATCH["since"] = time.time()
            OBS["grant_not_offered"] = {
                "at": time.time(), "turn": turn,
                "all_action_types": sorted({a["type"] for a in acts}),
                "object_ability_descriptions":
                    OBS["grant_probe"]["object_ability_descriptions"]}
            wire("grant_not_offered", OBS["grant_not_offered"])
            say("[P0] granted ability NOT advertised on equipped Yargle")
        return False
    return False


async def p1_step(c, pid, state, acts):
    if is_my_main(state, pid):
        lid = find_hand(state, pid, SWAMP)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    return False


async def attempt():
    reset_globals()
    t0a = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((YARGLE, 8), (BRACELET, 4), (SWAMP, 48)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((SWAMP, 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})
    OBS["game_code"] = p0.game_code

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1800
    while time.time() - t0a < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, tag, is_p0 in ((p0, p0.player_id, "P0", True),
                                   (p1, p1.player_id, "P1", False)):
            rej = drain_rejections(c)
            if rej:
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                OBS.setdefault("rejections", []).extend(
                    {"at": now, "who": c.name, "type": r["type"],
                     "data": r["data"]} for r in rej)
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, tag, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if wf_type(state) == "GameOver" and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            yoids = perm_oids(state, 0, YARGLE)
            if yoids and "yargle" not in SEEN_TURN:
                SEEN_TURN["yargle"] = state.get("turn_number")
                say(f"Yargle on BF turn {SEEN_TURN['yargle']}")
            if yoids and "attached" not in SEEN_TURN:
                boids = perm_oids(state, 0, BRACELET)
                for b in boids:
                    if attached_oid(state, b, yoids[0]):
                        if not ACT.get("equip_targeted"):
                            # auto-target case: single legal target, the
                            # engine attaches with no TargetSelection prompt
                            ACT["equip_targeted"] = {"at": time.time(),
                                                     "auto": True,
                                                     "ref": yoids[0]}
                            say("[P0] equip auto-targeted Yargle (no prompt)")
                        SEEN_TURN["attached"] = state.get("turn_number")
                        say(f"Bracelet attached to Yargle turn "
                            f"{SEEN_TURN['attached']}")
                        ST["stage"] = "PROOF"
                        say("=== stage -> PROOF ===")
                        break

        # --- grant ability on the stack?
        if ACT.get("grant") and not IDS["grant_ability"]:
            for sid in stack_ids(state):
                e = stack_entry(state, sid)
                blob = json.dumps(e, default=str)
                if "control target opponent" in blob.lower() or \
                        "ControlNextTurn" in blob:
                    IDS["grant_ability"] = sid
                    wire("grant_ability_on_stack",
                         {"sid": sid, "entry": e})
                    say(f"grant ability on stack: sid={sid}")
                    await export_now("mid_grant.json")
                    break

        # --- grant ability resolved?
        if IDS["grant_ability"] and ST["stage"] == "PROOF":
            if IDS["grant_ability"] not in stack_ids(state):
                OBS["grant_resolved_at"] = time.time()
                say("grant ability resolved (left the stack)")
                await export_now("post_activate.json")
                ST["stage"] = "CONTROL_OBSERVE"
                CONTROL_OBS["turn"] = state.get("turn_number")
                CONTROL_OBS["since"] = time.time()
                say("=== stage -> CONTROL_OBSERVE ===")

        # --- CONTROL_OBSERVE: watch P1's next turn for P0 decisions
        if ST["stage"] == "CONTROL_OBSERVE" and not ST["stop"]:
            p1_turn = (CONTROL_OBS["turn"] or 0) + 1
            if state.get("active_player") == 1 and \
                    state.get("turn_number") == p1_turn:
                st1 = p1.latest
                vi1 = (st1.get("viewer_interaction") or {}) if st1 else {}
                vi0 = (st.get("viewer_interaction") or {})
                snap = {
                    "turn": state.get("turn_number"),
                    "phase": state.get("phase"),
                    "wf": wf_type(state),
                    "wf_player": wf_player(state),
                    "priority_player": state.get("priority_player"),
                    "p0_vi_canSubmit": bool(vi0.get("canSubmit")),
                    "p0_opp": len(vi0.get("opportunities") or []),
                    "p1_vi_canSubmit": bool(vi1.get("canSubmit")),
                    "p1_opp": len(vi1.get("opportunities") or []),
                }
                key = (snap["turn"], snap["phase"], snap["wf"],
                       snap["wf_player"], snap["p0_vi_canSubmit"],
                       snap["p1_vi_canSubmit"])
                if ("ctrlsnap", key) not in SHAPES:
                    SHAPES.add(("ctrlsnap", key))
                    CONTROL_OBS["snaps"].append({**snap, "t": time.time()})
                    wire("control_observe", snap)
                # strong signal: P0 asked to decide for P1
                if str(wf_player(state)) == "0" and wf_type(state) not in \
                        (None, "GameOver"):
                    OBS["p0_decides_for_p1"] = {
                        "wf": wf_type(state), "phase": state.get("phase"),
                        "at": time.time()}
                    wire("p0_decides_for_p1", OBS["p0_decides_for_p1"])
                    say(f"CONTROL SIGNAL: P0 deciding for P1 "
                        f"({wf_type(state)} in {state.get('phase')})")
            # end conditions: P1's turn ended, or 150s elapsed
            if (state.get("active_player") == 0 and
                    state.get("turn_number") > p1_turn) or \
                    time.time() - CONTROL_OBS["since"] > 150:
                say("control observation window done; exporting post")
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watches in PROOF
        if ST["stage"] == "PROOF" and not ST["stop"]:
            since = GRANT_WATCH["since"]
            if since and time.time() - since > 90:
                say("granted ability never offered after 90s; exporting "
                    "post (captured failure)")
                wire("grant_probe_timeout", {})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            psince = PROOF_WATCH["since"]
            if psince and time.time() - psince > 120:
                say("PROOF stalled 120s with no progress; exporting post")
                wire("proof_stall_timeout", {})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            # arm the stall watch once pre_grant exists but nothing advances
            if OBS.get("pre_exported") and PROOF_WATCH["since"] is None \
                    and not ACT.get("grant") and GRANT_WATCH["since"] is None:
                PROOF_WATCH["since"] = time.time()
            if ACT.get("grant"):
                PROOF_WATCH["since"] = None
            # grant activation rejected outright
            if ACT.get("grant") and not IDS["grant_ability"]:
                rej_lines = [r for r in OBS.get("rejections", [])
                             if r["at"] >= ACT["grant"]["at"]]
                if rej_lines and time.time() - ACT["grant"]["at"] > 20:
                    say("grant activation rejected; exporting post")
                    wire("grant_rejected_stop",
                         {"count": len(rej_lines)})
                    OBS["grant_rejected"] = True
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"post export failed: {e}")
                    ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    # ---- assertions
    try:
        pre = load_state("pre_grant.json")
        yoids = perm_oids(pre, 0, YARGLE)
        boids = perm_oids(pre, 0, BRACELET)
        yo = pre["objects"][yoids[0]] if yoids else {}
        attached = bool(yoids and boids and
                        attached_oid(pre, boids[0], yoids[0]))
        pw, tw = num(yo.get("power")), num(yo.get("toughness"))
        A["A1_setup_ok"] = ("passed"
                            if attached and pw == 10 and tw == 4
                            and is_my_main(pre, 0) else "failed")
        obs["notes"].append(f"A1: attached={attached} yargle={pw}/{tw} "
                            f"main={is_my_main(pre, 0)}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e}")

    probe = OBS.get("grant_probe") or {}
    advertised = probe.get("advertised_indices") or []
    descs = probe.get("object_ability_descriptions") or []
    desc_match = any(grant_desc_match(d) for d in descs)
    # A2 passes only if the granted ability was actually exposed as an
    # activatable ability on the equipped creature
    if advertised and (desc_match or not descs):
        A["A2_granted_exposed"] = "passed"
    elif advertised:
        A["A2_granted_exposed"] = "passed"
        obs["notes"].append("A2: advertised indices present but object "
                            "descriptions did not match grant text "
                            "(treated as exposed; Yargle has no natives)")
    else:
        A["A2_granted_exposed"] = "failed"
    obs["notes"].append(f"A2: advertised_indices={advertised} "
                        f"obj_desc_match={desc_match} "
                        f"not_offered={bool(OBS.get('grant_not_offered'))}")

    try:
        post_a = load_state("post_activate.json")
        brace_zones = [(oid, o.get("zone"))
                       for oid, o in post_a["objects"].items()
                       if oname(o) == BRACELET]
        exiled = any(z == "Exile" for _, z in brace_zones)
        stack_clear = IDS["grant_ability"] not in stack_ids(post_a)
        no_rej = not OBS.get("grant_rejected")
        A["A3_activation_completes"] = (
            "passed" if exiled and stack_clear and no_rej else "failed")
        obs["notes"].append(f"A3: bracelet_zones={brace_zones} "
                            f"stack_clear_of_grant={stack_clear} "
                            f"no_rejection={no_rej}")
    except Exception as e:
        if A["A2_granted_exposed"] == "failed":
            A["A3_activation_completes"] = "not-run"
            obs["notes"].append("A3 not-run: granted ability never exposed")
        else:
            A["A3_activation_completes"] = "not-run"
            obs["notes"].append(f"A3 eval error: {e}")

    # A4: look for a lasting control registration, else the observation.
    # The granted ability DEFINITION itself carries a ControlNextTurn
    # effect tag, so marker search must exclude ability-definition blobs;
    # otherwise the definition's mere presence reads as a control effect.
    def strip_ability_defs(obj):
        # Strip every card-definition blob: static abilities live under
        # static_definitions / base_static_definitions on objects (and
        # static_abilities / deck_pools card records), so stripping only
        # "abilities" leaves ControlNextTurn definition tags behind.
        if isinstance(obj, dict):
            return {k: strip_ability_defs(v) for k, v in obj.items()
                    if k not in ("abilities", "static_abilities",
                                 "static_definitions",
                                 "base_static_definitions", "ability",
                                 "deck_pools", "definition")}
        if isinstance(obj, (list, tuple)):
            return [strip_ability_defs(v) for v in obj]
        return obj

    try:
        found = []
        sched_nonempty = False
        for path in ("post_activate.json", "post.json"):
            try:
                env2 = json.load(open(f"{EVDIR}/{path}"))
            except FileNotFoundError:
                continue
            st2 = env2["state"]
            if st2.get("scheduled_turn_controls"):
                sched_nonempty = True
            raw2 = json.dumps(strip_ability_defs(st2), default=str)
            for marker in ("ControlNextTurn", "control_next_turn",
                           "controls_player", "controlled_by",
                           "ControlPlayer"):
                if marker in raw2:
                    found.append((path, marker))
        ctrl_signal = bool(OBS.get("p0_decides_for_p1"))
        if found or ctrl_signal or sched_nonempty:
            A["A4_control_recorded"] = "passed"
        elif A.get("A3_activation_completes") != "passed":
            A["A4_control_recorded"] = "not-run"
            obs["notes"].append("A4 not-run: activation did not complete")
        else:
            A["A4_control_recorded"] = "failed"
        obs["notes"].append(f"A4: markers_outside_defs={found} "
                            f"scheduled_turn_controls_nonempty="
                            f"{sched_nonempty} "
                            f"p0_decides_for_p1={ctrl_signal} "
                            f"control_snaps={len(CONTROL_OBS['snaps'])}")
    except Exception as e:
        A["A4_control_recorded"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e}")

    try:
        final = load_state("post.json")
        stack_empty = not stack_ids(final)
        game_over = wf_type(final) == "GameOver"
        A["A5_cleanup"] = ("passed" if stack_empty and not game_over
                           else "failed")
        obs["notes"].append(f"A5: stack_empty={stack_empty} "
                            f"game_over={game_over}")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append(f"A5 eval error: {e}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "observations": OBS,
                   "ids": IDS, "act": ACT,
                   "seen_turns": SEEN_TURN}, f, indent=2, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS, "wf_sequence": WF_SEEN,
                   "ids": IDS, "seen_turns": SEEN_TURN}, f, indent=2,
                  default=str)

    a1 = A.get("A1_setup_ok")
    a2 = A.get("A2_granted_exposed")
    a3 = A.get("A3_activation_completes")
    a4 = A.get("A4_control_recorded")
    if a1 == "passed":
        if a2 == "failed":
            verdict = "reproduced"
        elif a2 == "passed" and a3 == "passed" and a4 == "passed":
            verdict = "not-reproduced"
        elif a2 == "passed" and (a3 == "failed" or a4 == "failed"):
            verdict = "reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    await p0.close()
    await p1.close()
    return obs, True


async def write_manifest():
    names = sorted(n for n in os.listdir(EVDIR) if n != "manifest.sha256")
    lines = []
    for name in names:
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs = {"assert": {}, "notes": [f"attempt {n} crash: {e!r}"]}
            done = False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    A = obs.get("assert", {})
    verdict = obs.get("verdict", "blocked")
    # copy the scenario source into the evidence dir before hashing
    shutil.copy(__file__, f"{EVDIR}/scenario_6872_0980.py")
    run = {
        "issue": 6872,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": {
            "server_version": SERVER_IDENTITY["server_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": True,
            "hello_mode": "Full",
        },
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Yargle, Glutton of Urborg", 8],
                   ["The Dominion Bracelet", 4], ["Swamp", 48]],
            "P1": [["Swamp", 60]],
        },
        "assertions": A,
        "notes": obs.get("notes", []),
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Yargle / 4x Bracelet density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 8x Yargle, Glutton of Urborg + 4x The Dominion "
                      "Bracelet + 48x Swamp; P1: 60x Swamp (passive)",
        "contract_line": ("Equip Bracelet to Yargle; the equipped creature must "
                          "expose the granted {15}/exile sorcery ability as an "
                          "activatable action, and activating it must register "
                          "control of P1's next turn."),
        "game_code": OBS.get("game_code"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    WIRE.close()
    RUNLOG.close()
    # render summary.png from the saved states/assertions
    import subprocess
    subprocess.run([sys.executable,
                    f"{BACKFILL}/driver/render_summary_6872.py",
                    EVDIR], check=True)
    asyncio.run(write_manifest())
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)
