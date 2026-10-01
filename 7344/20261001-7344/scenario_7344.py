#!/usr/bin/env python3
"""Issue #7344: Goblin Sledder doesn't correctly get +1/+1 until end of turn
when sacrificing Mogg War Marshal (reporter theorized Echo involvement).

Oracle (pinned v0.99.0 card data):
  Goblin Sledder {R} 1/1: "Sacrifice a Goblin: Target creature gets +1/+1
  until end of turn." (parser: Activated, cost Sacrifice Goblin x1,
  effect Pump +1/+1, target Typed Creature, UntilEndOfTurn)
  Mogg War Marshal {1}{R}: "Echo {1}{R} ... When this creature enters or
  dies, create a 1/1 red Goblin creature token." (parser: ChangesZone
  trigger -> Token Goblin 1/1 red; Echo NOT parsed - abilities: [])

Run: 20261001-7344 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  Leg 1 (the reported case): activate Sledder, sacrifice Mogg War Marshal,
  target the Sledder. Expect Sledder 1/1 -> 2/2 until EOT and a 1/1 red
  Goblin token from the dies trigger.
  Leg 2 (control): activate Sledder again, this time sacrificing a plain
  Goblin token (the Marshal ETB token), targeting the Sledder. Expect
  another +1/+1 (2/2 -> 3/3 if leg 1 worked).

  A1 setup_ok       pre.json: P0 PreCombatMain holding priority, Sledder +
                    Marshal on P0 BF, ETB Goblin token present, P1 Memnite
                    on BF, life 20/20, stack empty
  A2 activation_offered  engine advertises ActivateAbility for Sledder (leg 1)
  A3 target_and_cost     target chosen = Sledder; Marshal sacrificed (P0 GY)
  A4 pump_applied        post1 Sledder P/T = pre P/T + 1/+1 (REPORTED OUTCOME)
  A5 token_created       dies-trigger Goblin token on P0 BF in post1
  A6 control_pump        post2 Sledder P/T = post1 P/T + 1/+1 (token sacrifice)
  A7 cleanup             post2: stack empty, game proceeding

Verdict: reproduced iff A1 passes and (A2 or A3 or A4 fails) - the Sledder
does not gain +1/+1 when sacrificing the Marshal. not-reproduced iff the
pump lands on both legs.

Prompt disambiguation: the Pump's target prompt ("Target creature",
controller unrestricted) and the cost's sacrifice prompt ("Sacrifice a
Goblin", P0's Goblins only) are distinguished by candidate content: P1
controls a Memnite (not a Goblin), so an opportunity whose candidates
reference the Memnite is the target prompt; one referencing only P0
Goblins is the sacrifice prompt. This holds regardless of prompt order.
"""
import asyncio
import copy
import json
import os
import sys
import time
import hashlib

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7344"
ISSUE = 7344

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

SLEDDER = "Goblin Sledder"
MARSHAL = "Mogg War Marshal"
MOUNTAIN = "Mountain"
MEMNITE = "Memnite"
TOKEN = "Goblin"
# Dense threat playsets are a test-harness convenience (the engine accepts
# >4-of for custom games); they make the setup draw-reliable.
P0_DECK = [(SLEDDER, 8), (MARSHAL, 8), (MOUNTAIN, 44)]
P1_DECK = [(MEMNITE, 12), (MOUNTAIN, 48)]
LANDS = (MOUNTAIN,)
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
        "pre_exported": False, "mid1_exported": False, "mid2_exported": False,
        "post1_exported": False, "post2_exported": False,
        "rejections": [], "stop": False,
        "sledder_oid": None, "marshal_oid": None,
        "etb_token_oid": None, "death_token_oid": None,
        "p1_memnite_oid": None,
        "leg": 0,
        "target_chosen": {}, "sac_paid": {},
        "activation_offered": {}, "activation_submitted": {},
        "pre_pt": None, "post1_pt": None, "post2_pt": None,
        "hold_priority": False,
        "vi_wired": set(),
        "act_at": None,
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


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def bf_oid(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def goblin_tokens(state, pid):
    return [oid for oid, o in bf(state, pid) if oname(o) == TOKEN]


def pt_of(state, oid):
    o = state["objects"].get(str(oid), {})
    return (o.get("power"), o.get("toughness"))


def gy_names(state, pid):
    return [oname(o) for o in state["objects"].values()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action, "stage": ST.get("stage"),
                           "leg": ST.get("leg")})
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


async def do_mulligan(c, pid):
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
    n_lands = sum(1 for o in hand_oids(state, pid)
                  if oname(state["objects"][o]) in LANDS)
    has_key = any(oname(state["objects"][o]) in (SLEDDER, MARSHAL, MEMNITE)
                  for o in hand_oids(state, pid))
    n_mulls = _MULL_REV.get(c.name, 0)
    # P0 wants a Sledder; P1 wants a Memnite. Keep 2+ lands with the key
    # card, else mulligan (max 2), then keep on lands alone.
    if c.name == "P0":
        keep_ok = (n_lands >= 2 and has_key) or n_mulls >= 2 or \
            (n_lands >= 3 and n_mulls >= 1)
    else:
        keep_ok = n_lands >= 2 or n_mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        _MULL_REV[c.name] = n_mulls + 1
    _MULL_REV[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice}")
    return True


async def do_bottom(c, pid):
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
    h = hand_oids(state, pid)
    keep_names = {SLEDDER, MARSHAL, MEMNITE} | set(LANDS)
    pref = [o for o in h if oname(state["objects"][o]) not in keep_names]
    pref += [o for o in h if o not in pref]
    picks = [int(x) for x in pref[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid):
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
    keep_names = {SLEDDER, MARSHAL, MEMNITE} | set(LANDS)
    pref = [o for o in h if oname(state["objects"][o]) not in keep_names]
    pref += [o for o in h if o not in pref]
    picks = [int(x) for x in pref[:n]]
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


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def vi_choice_for_oid(st, oid, exclude_oid=None):
    """Find a viewer_interaction opportunity whose candidates reference oid.
    Returns (opportunity, choice) or None. Skips castSpell/priority menus.
    If exclude_oid is given, opportunities whose candidates reference it are
    skipped (they belong to a different prompt kind)."""
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        opp_blob = json.dumps(opp, default=str)
        if exclude_oid and str(exclude_oid) in opp_blob:
            continue
        for ch in chs:
            if not isinstance(ch, dict):
                continue
            codes = action_codes(ch)
            if "castSpell" in codes or "passPriority" in codes:
                continue
            blob = json.dumps(ch, default=str)
            if str(oid) in blob:
                return opp, ch
    return None


def vi_opp_referencing(st, *oids):
    """Find the first vi opportunity (non castSpell/passPriority menu) whose
    full blob references ALL of the given oids. Returns (opp, data)."""
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if any("castSpell" in action_codes(ch) or "passPriority" in action_codes(ch)
               for ch in chs if isinstance(ch, dict)):
            continue
        blob = json.dumps(opp, default=str)
        if all(str(o) in blob for o in oids):
            return opp, data
    return None


def submit_vi(c, opp, ch):
    iid = opp.get("interactionId") or opp.get("id")
    cid = ch.get("id") or ch.get("choiceId")
    spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if not iid or cid is None:
        return None
    # Response type must mirror the spec variant: the engine's availability
    # witness is authoritative. Observed: "sequence" -> sequence/choiceIds,
    # "select" -> select/choiceIds (a "choose" here is rejected as
    # invalid_interaction_response), otherwise exactChoices -> choose/choiceId.
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
    elif stype == "select":
        sub = {"interactionId": iid,
               "response": {"type": "select", "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    return sub, iid, cid, stype


async def answer_target(c, pid, state, st, leg):
    """Answer the Pump's target prompt: choose the Sledder. The target
    prompt's candidates include P1's Memnite (any creature); the sacrifice
    prompt's candidates are P0 Goblins only. So only answer opportunities
    whose candidates reference the Memnite."""
    if ST["target_chosen"].get(leg):
        return False
    p1m = ST.get("p1_memnite_oid")
    sled = ST.get("sledder_oid")
    if not p1m or not sled:
        return False
    # the target prompt's candidates include the Memnite (any creature);
    # the sacrifice prompt's never do. Find the opportunity referencing both.
    found = vi_opp_referencing(st, sled, p1m)
    if not found:
        return False
    opp, data = found
    chs = data.get("choices") or data.get("candidates") or []
    pick = None
    for chx in chs:
        if isinstance(chx, dict) and str(sled) in json.dumps(chx, default=str):
            if "castSpell" in action_codes(chx) or "passPriority" in action_codes(chx):
                continue
            pick = chx
            break
    if pick is None:
        wire("target_no_sledder_choice", {"leg": leg})
        return False
    res = submit_vi(c, opp, pick)
    if not res:
        wire("target_vi_unusable", {"leg": leg})
        return False
    sub, iid, cid, stype = res
    wire("target_submit", {"leg": leg, "iid": iid, "cid": cid, "stype": stype,
                           "target": sled})
    await c.send_interaction(sub)
    ST["target_chosen"][leg] = True
    say(f"[P0] leg{leg}: target prompt answered -> Sledder {sled} (stype={stype})")
    return True


async def answer_sacrifice(c, pid, state, st, leg, victim_oid):
    """Answer the sacrifice-cost prompt with victim_oid. Never touch an
    opportunity whose candidates reference P1's Memnite (that's the target
    prompt)."""
    if ST["sac_paid"].get(leg):
        return False
    p1m = ST.get("p1_memnite_oid")
    for a in st.get("legal_actions", []) or []:
        blob = json.dumps(a, default=str)
        if "sacrifice" in (a.get("type") or "").lower() and str(victim_oid) in blob:
            if p1m and str(p1m) in blob:
                continue
            wire("sacrifice_action", {"leg": leg, "action": a})
            await submit_as_is(c, a)
            ST["sac_paid"][leg] = True
            say(f"[P0] leg{leg}: sacrifice via advertised {a.get('type')} (victim {victim_oid})")
            return True
    vcc = vi_choice_for_oid(st, victim_oid, exclude_oid=p1m)
    if not vcc:
        return False
    opp, ch = vcc
    res = submit_vi(c, opp, ch)
    if not res:
        wire("sac_vi_unusable", {"leg": leg})
        return False
    sub, iid, cid, stype = res
    wire("sacrifice_submit", {"leg": leg, "iid": iid, "cid": cid,
                              "stype": stype, "victim": victim_oid})
    await c.send_interaction(sub)
    ST["sac_paid"][leg] = True
    say(f"[P0] leg{leg}: sacrificed {victim_oid} (stype={stype})")
    return True


async def handle_unless_payment(c, pid, state, st):
    """Answer an UnlessPayment prompt (Mogg War Marshal's Echo {1}{R}).
    Pay when P0 can afford it (2 untapped Mountains); otherwise decline.
    Wires the full opportunity shape on first sight for diagnosis."""
    if wf_type(state) != "UnlessPayment":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    data = wf_data(state)
    src = (data.get("pending_effect") or {}).get("source_id")
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        key = f"unless:{iid}"
        if key not in ST["vi_wired"]:
            ST["vi_wired"].add(key)
            wire("unless_shape", {"iid": iid, "wf_data": data, "opp": opp})
            say(f"[P0] UnlessPayment shape wired (iid={iid}, src={src})")
    # classify choices into pay / decline by text/surfaces
    pay_ch, dec_ch = None, None
    the_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        chs = rdata.get("choices") or rdata.get("candidates") or []
        if not chs:
            continue
        the_opp = opp
        for ch in chs:
            if not isinstance(ch, dict):
                continue
            codes = action_codes(ch)
            if "castSpell" in codes or "passPriority" in codes:
                continue
            txt = choice_text(ch).lower()
            blob = json.dumps(ch, default=str).lower()
            if any(k in txt or k in blob for k in
                   ("decline", "don't pay", "do not pay", "sacrifice")):
                dec_ch = dec_ch or ch
            else:
                pay_ch = pay_ch or ch
    if the_opp is None:
        return False
    n_untapped = untapped_of(state, pid, MOUNTAIN)
    # echo cost is {1}{R}: payable with 2 untapped Mountains
    if pay_ch is not None and n_untapped >= 2:
        res = submit_vi(c, the_opp, pay_ch)
        if res:
            sub, iid, cid, stype = res
            wire("unless_pay", {"iid": iid, "cid": cid, "stype": stype,
                                "text": choice_text(pay_ch)[:80],
                                "untapped_mountains": n_untapped})
            await c.send_interaction(sub)
            ST["echo_paid"] = True
            say(f"[P0] UnlessPayment: PAYING echo (stype={stype}, "
                f"{n_untapped} untapped Mountains)")
            return True
    if dec_ch is not None:
        res = submit_vi(c, the_opp, dec_ch)
        if res:
            sub, iid, cid, stype = res
            wire("unless_decline", {"iid": iid, "cid": cid, "stype": stype,
                                   "text": choice_text(dec_ch)[:80]})
            await c.send_interaction(sub)
            ST["echo_declined"] = True
            ST["marshal_oid"] = None  # sacrificed; setup may recast later
            say("[P0] UnlessPayment: DECLINING echo (cannot afford) - "
                "Marshal will be sacrificed")
            return True
    wire("unless_no_answer", {"n_untapped": n_untapped,
                              "pay_found": pay_ch is not None,
                              "dec_found": dec_ch is not None})
    return False


async def handle_mana_payment(c, pid, state, st):
    """Submit advertised PayMana/PayManaAbilityMana actions as-is (e.g. the
    follow-up to paying an UnlessPayment mana cost)."""
    if wf_type(state) != "ManaPayment":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    for a in st.get("legal_actions", []) or []:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            wire("mana_pay_action", {"action": a, "stage": ST.get("stage")})
            await submit_as_is(c, a)
            say(f"[P0] ManaPayment: submitted {a.get('type')}")
            return True
    return False


async def p_land_drop(c, pid, state, acts):
    if state.get("land_played_this_turn"):
        return False
    lid = find_hand(state, pid, MOUNTAIN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == str(lid):
                await submit_as_is(c, a)
                say(f"[{c.name}] land drop Mountain")
                return True
    return False


async def p_cast_creature(c, pid, state, acts, name):
    if bf_oid(state, pid, name):
        return False
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
            return True
    # No advertised CastSpell yet: wait for the next revision rather than
    # submitting a raw fallback (raw fallbacks get rejected and pollute
    # the rejection record the cleanup assertion reads).
    return False


def clear_inflight(state, pid):
    cif = ST.get("cast_in_flight")
    if cif and not any(str(oid) == str(cif) for oid in hand_oids(state, pid)):
        ST["cast_in_flight"] = None
        wire("cast_left_hand", {"oid": cif})
        say(f"cast in-flight {cif} left hand")


def activation_advertised(acts, sledder_oid):
    out = []
    for a in acts:
        if a.get("type") == "ActivateAbility" and str(
                (a.get("data") or {}).get("source_id")) == str(sledder_oid):
            out.append(a)
    return out


def wire_vi_snapshot(c, pid, st, why):
    vi = get_vi(st)
    if not vi:
        return
    key = why + ":" + json.dumps(vi, default=str)[:200]
    if key in ST["vi_wired"]:
        return
    ST["vi_wired"].add(key)
    wire("p0_vi", {"why": why, "wf": wf_type(st["state"]), "vi": vi,
                   "stage": ST.get("stage"), "leg": ST.get("leg")})
    say(f"[P0] vi ({why}) wf={wf_type(st['state'])} stage={ST.get('stage')}")


async def p0_tick(c, pid):
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
    # Echo safety net: answer UnlessPayment (pay when affordable), then any
    # follow-up ManaPayment, before anything else.
    if await handle_unless_payment(c, pid, state, st):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True

    stage = ST["stage"]
    leg = ST.get("leg", 0)
    if stage in ("ACT1", "ACT2") and get_vi(st) and str(wf_player(state)) == str(pid):
        if await answer_target(c, pid, state, st, leg):
            return True
        victim = ST.get("marshal_oid") if leg == 1 else ST.get("etb_token_oid")
        if victim and await answer_sacrifice(c, pid, state, st, leg, victim):
            return True
        wire_vi_snapshot(c, pid, st, "unanswered")

    if stage == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast_creature(c, pid, state, acts, SLEDDER):
            return True
        # Gate the Marshal on the Sledder being on the battlefield: leg 1
        # (sacrifice the Marshal to the Sledder) must fire in the SAME main
        # phase as the Marshal's cast, before the next upkeep's Echo
        # (UnlessPayment) can touch it.
        if bf_oid(state, pid, SLEDDER):
            if await p_cast_creature(c, pid, state, acts, MARSHAL):
                return True
        if await p_land_drop(c, pid, state, acts):
            return True
        sled = bf_oid(state, pid, SLEDDER)
        marshal = bf_oid(state, pid, MARSHAL)
        if sled:
            ST["sledder_oid"] = sled
        if marshal:
            ST["marshal_oid"] = marshal
        # detect the Marshal ETB token (a P0 "Goblin" on the BF)
        if marshal and not ST.get("etb_token_oid"):
            toks = goblin_tokens(state, pid)
            if toks:
                ST["etb_token_oid"] = toks[0]
                wire("etb_token", {"oid": toks[0]})
                say(f"[P0] ETB Goblin token detected: {toks[0]}")
        p1m = bf_oid(state, 1, MEMNITE)
        if p1m:
            ST["p1_memnite_oid"] = p1m
        stack_empty = len(state.get("stack") or []) == 0
        ready = (sled and marshal and ST.get("etb_token_oid") and p1m
                 and stack_empty and not ST.get("pre_exported"))
        if ready:
            s = await export_now(c, "pre.json")
            if s is not None:
                ST["pre_exported"] = True
                ps = json.loads(s)["state"]
                ST["pre_pt"] = pt_of(ps, sled)
                wire("pre", {"sledder_oid": sled, "marshal_oid": marshal,
                             "etb_token_oid": ST["etb_token_oid"],
                             "p1_memnite_oid": p1m, "pre_pt": ST["pre_pt"],
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase")})
                say(f"[P0] pre exported; Sledder P/T={ST['pre_pt']}")
            acts_now = (c.latest or {}).get("legal_actions", [])
            aa = activation_advertised(acts_now, sled)
            if aa:
                ST["activation_offered"][1] = True
                wire("activation_advertised", {"leg": 1, "action": aa[0]})
                say("[P0] leg1: Sledder ActivateAbility offered - submitting")
                await submit_as_is(c, aa[0])
                ST["activation_submitted"][1] = True
                ST["leg"] = 1
                ST["stage"] = "ACT1"
                ST["act_at"] = time.time()
                return True
            wire("no_activation_offer", {
                "flat_types": sorted(set(x.get("type") for x in acts_now))})
            say("[P0] leg1: Sledder ActivateAbility NOT offered - BUG CANDIDATE")
            ST["no_offer_observations"] = ST.get("no_offer_observations", 0) + 1
            if ST["no_offer_observations"] >= 4:
                ST["hold_priority"] = True
                try:
                    await export_now(c, "post.json")
                finally:
                    ST["hold_priority"] = False
                ST["post2_exported"] = True
                ST["stop"] = True
                return True
        elif sled and marshal and not ST.get("pre_exported"):
            aa = activation_advertised(acts, sled)
            if not aa:
                wire("no_activation_offer_early", {
                    "flat_types": sorted(set(x.get("type") for x in acts)),
                    "stack": len(state.get("stack") or [])})
    if stage == "BETWEEN" and is_my_main(state, pid) and my_priority(state, pid):
        sled = ST.get("sledder_oid")
        etb = ST.get("etb_token_oid")
        # the ETB token must still be on the battlefield for the control leg
        etb_alive = any(str(oid) == str(etb) for oid, o in bf(state, pid))
        stack_empty = len(state.get("stack") or []) == 0
        if sled and etb and etb_alive and stack_empty:
            aa = activation_advertised(acts, sled)
            if aa:
                ST["activation_offered"][2] = True
                wire("activation_advertised", {"leg": 2, "action": aa[0]})
                say("[P0] leg2 (control): Sledder ActivateAbility offered - submitting")
                await submit_as_is(c, aa[0])
                ST["activation_submitted"][2] = True
                ST["leg"] = 2
                ST["stage"] = "ACT2"
                ST["act_at"] = time.time()
                return True
            wire("no_activation_offer_leg2", {
                "flat_types": sorted(set(x.get("type") for x in acts))})
            say("[P0] leg2: Sledder ActivateAbility NOT offered")
            ST["stop"] = True
            return True
    if state.get("active_player") == pid and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if await pass_priority(c, pid):
        return True
    return False


async def p1_tick(c, pid):
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
    if is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast_creature(c, pid, state, acts, MEMNITE):
            return True
        if await p_land_drop(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if await pass_priority(c, pid):
        return True
    return False


def sac_done(state, pid, victim_oid, victim_name):
    if not victim_oid:
        return False
    on_bf = any(str(oid) == str(victim_oid) for oid, _ in bf(state, pid))
    if on_bf:
        return False
    obj = state["objects"].get(str(victim_oid))
    # A sacrificed card lands in the graveyard; a sacrificed token may
    # instead cease to exist entirely (normal MTG token behavior) - either
    # outcome proves the sacrifice was paid.
    if obj is None:
        wire("victim_ceased_to_exist", {"victim": victim_oid})
        return True
    return obj.get("zone") == "Graveyard"


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p1_seat": p1.player_id,
                          "p0_deck": P0_DECK, "p1_deck": P1_DECK})
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
                # A rejection during an activation leg means a target/sacrifice
                # interaction response was refused: clear the optimistic flags
                # so the driver re-answers the still-pending prompt instead of
                # stalling on a believed-done step. (Setup-stage rejections do
                # not touch leg flags.)
                if ST.get("stage") in ("ACT1", "ACT2"):
                    leg_now = ST.get("leg")
                    ST["target_chosen"][leg_now] = False
                    ST["sac_paid"][leg_now] = False
                    wire("prompt_flags_cleared",
                         {"leg": leg_now, "n_rej": len(rej)})
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

        # ---- leg 1 checkpoints ----
        if stage == "ACT1":
            if sac_done(state, _p0, ST.get("marshal_oid"), MARSHAL) \
                    and not ST.get("mid1_exported"):
                s = await export_now(p0, "mid1.json")
                if s is not None:
                    ST["mid1_exported"] = True
                    wire("mid1", {"stack_n": len(state.get("stack") or []),
                                  "wf": wf_type(state)})
                    say(f"mid1: Marshal sacrificed, stack={len(state.get('stack') or [])}")
                    ST["stage"] = "RES1"
                    stage = "RES1"
            if not ST.get("post1_exported") and now - ST.get("act_at", now) > 150:
                say("leg1 activation stalled 150s; exporting post1 and stopping")
                wire("leg1_stalled", {"wf": wf_type(state)})
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post1.json")
                finally:
                    ST["hold_priority"] = False
                ST["post1_exported"] = True
                ST["stop"] = True
        if stage == "RES1":
            # detect the dies-trigger token (a new P0 Goblin token)
            if not ST.get("death_token_oid"):
                known = {str(ST.get("etb_token_oid"))}
                for oid in goblin_tokens(state, _p0):
                    if str(oid) not in known:
                        ST["death_token_oid"] = oid
                        wire("death_token", {"oid": oid})
                        say(f"[P0] death-trigger Goblin token detected: {oid}")
                        break
            stack = state.get("stack") or []
            if not stack and ST.get("sac_paid").get(1) and not ST.get("post1_exported"):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post1.json")
                    if s is not None:
                        ST["post1_exported"] = True
                        ps = json.loads(s)["state"]
                        ST["post1_pt"] = pt_of(ps, ST.get("sledder_oid"))
                        say(f"post1: leg1 resolved, Sledder P/T={ST['post1_pt']}")
                finally:
                    ST["hold_priority"] = False
                ST["stage"] = "BETWEEN"
                stage = "BETWEEN"
            if not ST.get("post1_exported") and now - ST.get("act_at", now) > 240:
                say("leg1 resolving stalled 240s; exporting post1 and stopping")
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post1.json")
                finally:
                    ST["hold_priority"] = False
                ST["post1_exported"] = True
                ST["stop"] = True

        # ---- leg 2 checkpoints ----
        if stage == "ACT2":
            if sac_done(state, _p0, ST.get("etb_token_oid"), TOKEN) \
                    and not ST.get("mid2_exported"):
                s = await export_now(p0, "mid2.json")
                if s is not None:
                    ST["mid2_exported"] = True
                    wire("mid2", {"stack_n": len(state.get("stack") or []),
                                  "wf": wf_type(state)})
                    say(f"mid2: ETB token sacrificed, stack={len(state.get('stack') or [])}")
                    ST["stage"] = "RES2"
                    stage = "RES2"
            if not ST.get("post2_exported") and now - ST.get("act_at", now) > 150:
                say("leg2 activation stalled 150s; exporting post2 and stopping")
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post2.json")
                finally:
                    ST["hold_priority"] = False
                ST["post2_exported"] = True
                ST["stop"] = True
        if stage == "RES2":
            stack = state.get("stack") or []
            if not stack and ST.get("sac_paid").get(2) and not ST.get("post2_exported"):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post2.json")
                    if s is not None:
                        ST["post2_exported"] = True
                        ps = json.loads(s)["state"]
                        ST["post2_pt"] = pt_of(ps, ST.get("sledder_oid"))
                        say(f"post2: leg2 resolved, Sledder P/T={ST['post2_pt']}")
                finally:
                    ST["hold_priority"] = False
                ST["stage"] = "DONE"
                ST["stop"] = True
            if not ST.get("post2_exported") and now - ST.get("act_at", now) > 240:
                say("leg2 resolving stalled 240s; exporting post2 and stopping")
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post2.json")
                finally:
                    ST["hold_priority"] = False
                ST["post2_exported"] = True
                ST["stop"] = True

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        if now - ST.get("last_diag", 0) > 30:
            ST["last_diag"] = now
            hand0 = [oname(state["objects"][o]) for o in hand_oids(state, _p0)]
            bf0 = [oname(o) for _, o in bf(state, _p0)]
            bf1 = [oname(o) for _, o in bf(state, _p1)]
            stack = state.get("stack") or []
            say(f"DIAG turn={state.get('turn_number')} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"p0hand={len(hand0)} p0bf={bf0} p1bf={bf1} "
                f"stage={ST.get('stage')} leg={ST.get('leg')} "
                f"tgt={ST['target_chosen']} sac={ST['sac_paid']}")
            wire("diag", {"turn": state.get("turn_number"), "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "p0hand": hand0, "p0bf": bf0, "p1bf": bf1})

        if not ST.get("post2_exported") and now - t0 > TIMEOUT - 120:
            await export_now(p0, "post2.json")
            ST["post2_exported"] = True
            ST["stop"] = True

    await p0.close()
    await p1.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list, set)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
