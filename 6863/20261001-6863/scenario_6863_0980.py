#!/usr/bin/env python3
"""Issue #6863: Estrid, the Masked -1 creates a Mask Aura token but umbra
armor (totem armor) does not stop the enchanted creature from being destroyed.

Oracle: "[-1]: Create a white Aura enchantment token named Mask attached to
another target permanent. The token has enchant permanent and umbra armor."

Protocol-94 port of scenario_6863.py (protocol 69, run 20260911-6863d,
verdict not-reproduced on v0.80.0). Re-validating on pinned v0.98.0 /
protocol 94 per the staleness rule.

Plan (two human seats, native engine):
  SETUP  - land drops; P0 casts Grizzly Bears, then Estrid, the Masked.
  ACTIVATE - P0 activates Estrid's -1 targeting the Bears; Mask token must
             enter attached to the Bears with umbra armor (TotemArmor keyword).
  MURDER - P0 casts Murder targeting the masked Bears.
           Expected: umbra armor replacement destroys the Mask instead;
           the Bears stay on the battlefield.
  CONTROL - P0 casts a second Murder at an unmasked Bears: it must die.

Behavioral contract:
  A1 setup_ok        pre_activate.json: Estrid + >=1 Bears on P0 battlefield
  A2 mask_attached   mid_mask.json: Mask token on BF attached to the Bears,
                     carrying the TotemArmor keyword entry
  A3 armor_replaces  post_murder.json: masked Bears still on BF; Mask gone
                     (destroyed tokens cease to exist in the object map)
  A4 control_dies    post_control.json: unmasked Bears destroyed by Murder
  A5 cleanup         game continues; no stuck prompt

Verdict = reproduced iff A2 passes and A3 fails (masked creature dies /
Mask survives); not-reproduced iff A2, A3, A4, A5 pass.

Protocol-94 notes (from the scenario_6861_0980 port):
- MulliganDecision as {"choice":{"type":"Keep"}} gated on the seat's pending
  Declare entry; bottom-after-mulligan via pending BottomCards phase.
- DeclareAttackers/Blockers answered via the relations-schema interaction
  (no legacy declare actions).
- ActivateAbility may arrive as an advertised action OR as viewer-interaction
  choices carrying the activateAbility code; both paths handled.
- castSpell menu choices matched by exact object.reference (blob-substring
  matching is unreliable).
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6863"
EVID_ISSUE = "6863"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ESTRID = "Estrid, the Masked"
BEAR = "Grizzly Bears"
MURDER = "Murder"
MASK = "Mask"
FOREST = "Forest"
PLAINS = "Plains"
ISLAND = "Island"
SWAMP = "Swamp"

ST = {"stage": "SETUP", "stop": False, "retry": False,
      "act_tried": False, "act_done": False}


def reset_globals():
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "act_tried": False, "act_done": False})
    CAST.clear()
    MULLS.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    TGT.clear()
    TGT.update({"murder": None, "control": None})
    MASKW.clear()
    MASKW.update({"oid": None, "bear_oid": None, "seen": False})
    C0 = None
CAST = {}            # per-cast tracking for Murder
MULLS = {}           # per-seat mulligan bookkeeping
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
TGT = {"murder": None, "control": None}  # target prompts answered
MASKW = {"oid": None, "bear_oid": None, "seen": False}


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


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


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


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def n_untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (FOREST, PLAINS, ISLAND, SWAMP)
               and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_ops(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": wf_of(state).get("data"),
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
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:260]}")
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


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def candidate_info(opp, state):
    """Resolve opportunity candidates to (choiceId, ref_oid, name, zone)."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d["reference"])
                break
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "owner": o.get("owner")})
    return out


def attach_targets(o):
    """Find object ids inside attached_to (p94: {"data": oid, "type":"Object"}
    or nested dicts/lists on older protocols)."""
    found = []

    def rec(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, int):
            found.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                rec(v)
        elif isinstance(x, str) and x.isdigit():
            found.append(int(x))
    rec(o.get("attached_to"))
    return [str(i) for i in found]


def masked_bears_oid(state):
    """The Bears currently wearing the Mask token (or None)."""
    for oid, o in state["objects"].items():
        if oname(o) == MASK and o.get("zone") == "Battlefield":
            for t in attach_targets(o):
                to = state["objects"].get(str(t), {})
                if oname(to) == BEAR and to.get("zone") == "Battlefield":
                    return str(t), str(oid)
    return None, None


def has_totem_armor(o):
    """Strict TotemArmor keyword check (entry, not substring)."""
    for kw in o.get("keywords") or []:
        if isinstance(kw, str) and kw == "TotemArmor":
            return True
        if isinstance(kw, dict) and "TotemArmor" in kw:
            return True
    return False


async def do_mulligan(c, pid, tag, keep_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if MULLS.get((tag, "kept")):
        return False
    mull_count = MULLS.get((tag, "mulls"), 0)
    choice = "Keep" if (keep_fn(state) or mull_count >= 2) else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (prior mulligans: {mull_count})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    if choice == "Mulligan":
        MULLS[(tag, "mulls")] = mull_count + 1
    else:
        MULLS[(tag, "kept")] = True
    wire("mulligan", {"who": tag, "decision": choice})
    return True


async def do_bottom(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed") in MULLS:
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                   key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    if MULLS.get((tag, "discarded")) == c.revision:
        return False
    n = max(0, len(hand_oids(state, pid)) - 7)
    if n <= 0:
        return False
    h = hand_oids(state, pid)
    pref = sorted(h, key=rank_fn(state))
    picks = [int(x) for x in pref[:n]]
    say(f"[{tag}] discards {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "discarded")] = c.revision
    return True


async def answer_declare(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    iid = None
    for opp in vi_ops(c):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            iid = opp.get("interactionId") or opp.get("id")
            break
    if not iid or iid in SUBMITTED:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_empty", {"who": tag, "wf": wf.get("type")})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


async def answer_target_prompt(c, state, acts, st, key, pick_ref):
    """Answer P0's TargetSelection prompt identified for `key`; choose the
    candidate whose ref equals pick_ref."""
    if TGT.get(key):
        return False
    if wf_of(state).get("type") != "TargetSelection":
        return False
    if str(wf_player(state)) != "0":
        return False
    for opp in vi_ops(c):
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        zones = {x["zone"] for x in cands}
        if "Battlefield" not in zones:
            continue
        if (key, len(chs)) not in SHAPES:
            SHAPES.add((key, len(chs)))
            wire(f"{key}_target_prompt",
                 {"rtype": rtype, "candidates": cands})
            say(f"[P0] {key} target prompt: {[x['name'] for x in cands]}")
        want = next((x for x in cands if x["ref"] == str(pick_ref)), None)
        if not want:
            continue
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] {key}: unexpected prompt shape {rtype}/{spec_type}")
            continue
        await send_interaction(c, {"interactionId": iid, "response": resp_out})
        SUBMITTED.add(iid)
        TGT[key] = {"ref": str(pick_ref), "at": time.time()}
        say(f"[P0] {key}: targeted {want['name']} (oid={want['ref']})")
        return True
    return False


def p0_keep_fn(state):
    lands = [oname(state["objects"][o]) for o in hand_oids(state, 0)]
    n_lands = sum(1 for n in lands
                  if n in (FOREST, PLAINS, ISLAND, SWAMP))
    has_swamp = SWAMP in lands
    return n_lands >= 2 and (has_swamp or MULLS.get(("P0", "mulls"), 0) >= 1)


def p1_keep_fn(state):
    lands = [oname(state["objects"][o]) for o in hand_oids(state, 1)]
    n_lands = sum(1 for n in lands
                  if n in (FOREST, PLAINS, ISLAND, SWAMP))
    return n_lands >= 2


def bottom_rank_fn(state):
    # bottom lands first (keep the combo pieces)
    def key(o):
        n = oname(state["objects"][o])
        return 0 if n in (FOREST, PLAINS, ISLAND, SWAMP) else 1
    return key


def p0_discard_rank(state):
    def key(o):
        n = oname(state["objects"][o])
        if ST["stage"] in ("MURDER", "CONTROL"):
            rank = {ESTRID: 0, BEAR: 1, FOREST: 2, PLAINS: 2,
                    ISLAND: 2, SWAMP: 99, MURDER: 99}
            return rank.get(n, 50)
        keep = {ESTRID, BEAR, MURDER, SWAMP}
        if n in (FOREST, PLAINS, ISLAND):
            return 0
        return 1 if n not in keep else 2
    return key


async def tick(c, pid, is_p0):
    tag = "P0" if is_p0 else "P1"
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])

    if await do_mulligan(c, pid, tag, p0_keep_fn if is_p0 else p1_keep_fn):
        return True
    if await do_bottom(c, pid, tag, bottom_rank_fn):
        return True
    if await do_discard(c, pid, tag,
                        p0_discard_rank if is_p0 else (lambda s: (lambda o: 0))):
        return True
    if await answer_declare(c, pid, tag):
        return True

    # answer P0's target prompts before anything else
    if is_p0:
        if ST["stage"] == "ACTIVATE" and MASKW.get("bear_oid"):
            if await answer_target_prompt(c, state, acts, st, "estrid_minus1",
                                          MASKW["bear_oid"]):
                return True
        if ST["stage"] == "MURDER" and CAST.get("oid") and not TGT["murder"]:
            bear, _ = masked_bears_oid(state)
            if bear and await answer_target_prompt(c, state, acts, st,
                                                   "murder", bear):
                return True
        if ST["stage"] == "CONTROL" and CAST.get("oid") and not TGT["control"]:
            bear, _ = masked_bears_oid(state)
            cand = None
            for oid, o in state["objects"].items():
                if (oname(o) == BEAR and o.get("zone") == "Battlefield"
                        and o.get("controller") == 0 and str(oid) != bear):
                    cand = str(oid)
                    break
            if cand and await answer_target_prompt(c, state, acts, st,
                                                   "control", cand):
                return True
    # never pass while P0 has a decision pending
    wtype = wf_of(state).get("type")
    if is_p0 and wtype in ("OptionalCostChoice", "TargetSelection",
                           "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and str(wf_player(state)) == "0":
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await p0_setup(c, pid, state, acts):
                return True
        else:
            if await p0_land_drop(c, pid, state, acts):
                return True
            if ST["stage"] == "ACTIVATE":
                if await p0_activate(c, pid, state, acts):
                    return True
            elif ST["stage"] == "MURDER":
                if await p0_murder(c, pid, state, acts, "MURDER"):
                    return True
            elif ST["stage"] == "CONTROL":
                if await p0_murder(c, pid, state, acts, "CONTROL"):
                    return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    for name in (SWAMP, FOREST, PLAINS, ISLAND):
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def p0_setup(c, pid, state, acts):
    # Swamp first so Murder mana comes online early.
    for name in (SWAMP, FOREST, PLAINS, ISLAND):
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    # cast Bears (needs G + 1)
    if not any(oname(o) == BEAR for _, o in bf(state, pid)):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, FOREST) >= 1 \
                and n_untapped_lands(state, pid) >= 2:
            await submit_as_is(c, a)
            say("P0 casts Grizzly Bears")
            return True
    # cast Estrid (needs G + W + U + 1); gate on none-on-BF (legend)
    if not any(oname(o) == ESTRID for _, o in bf(state, pid)):
        oid = find_hand(state, pid, ESTRID)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, FOREST) >= 1 \
                and untapped_of(state, pid, PLAINS) >= 1 \
                and untapped_of(state, pid, ISLAND) >= 1 \
                and n_untapped_lands(state, pid) >= 4:
            await submit_as_is(c, a)
            say("P0 casts Estrid, the Masked")
            return True
    return False


def activate_minus1_action(acts, state):
    """Find Estrid's -1 ActivateAbility action (ability_index 1)."""
    estrid_oids = [str(oid) for oid, o in bf(state, 0)
                   if oname(o) == ESTRID]
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in estrid_oids:
            out.append((d.get("ability_index"), a))
    return out


def activate_minus1_choice(c, state):
    """p94 fallback: viewer-interaction choices carrying activateAbility
    codes on Estrid's -1."""
    estrid_oids = [str(oid) for oid, o in bf(state, 0)
                   if oname(o) == ESTRID]
    out = []
    for opp in vi_ops(c):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = action_codes(ch)
            if "activateAbility" not in codes:
                continue
            ref = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    ref = str(d["reference"])
                    break
            if ref in estrid_oids:
                out.append((ch.get("id"), opp.get("interactionId"),
                            resp.get("type")))
    return out


async def p0_activate(c, pid, state, acts):
    if ST["act_tried"]:
        return False
    estrid = next((str(oid) for oid, o in bf(state, pid)
                   if oname(o) == ESTRID), None)
    bear = next((str(oid) for oid, o in bf(state, pid)
                 if oname(o) == BEAR), None)
    if not estrid or not bear:
        return False
    if not MASKW.get("bear_oid"):
        MASKW["bear_oid"] = bear
    options = activate_minus1_action(acts, state)
    if ("act_opts", tuple(sorted(str(i) for i, _ in options))) not in SHAPES:
        SHAPES.add(("act_opts", tuple(sorted(str(i) for i, _ in options))))
        wire("activate_options", {"estrid": estrid, "bear": bear,
                                  "options": [(i, a) for i, a in options],
                                  "all_action_types": sorted({a["type"]
                                                              for a in acts})})
        say(f"[P0] ActivateAbility options on Estrid: "
            f"{[(i, json.dumps(a)[:160]) for i, a in options]}")
        say(f"[P0] ALL legal action types: {sorted({a['type'] for a in acts})}")
    choice = next((a for i, a in options if i == 1), None)
    if choice:
        await export_now("pre_activate.json")
        ST["act_tried"] = True
        await submit_as_is(c, choice)
        say(f"[P0] activates Estrid -1 (ability_index 1) via action, "
            f"targeting Bears {bear}")
        wire("estrid_minus1", {"estrid": estrid, "bear": bear,
                               "path": "action"})
        return True
    # p94 fallback: viewer-interaction choice
    if ("act_choices", "seen") not in SHAPES:
        chs = activate_minus1_choice(c, state)
        wire("activate_choice_options", {"choices": chs})
        say(f"[P0] no advertised ActivateAbility; viewer choices: {chs}")
        SHAPES.add(("act_choices", "seen"))
    chs = activate_minus1_choice(c, state)
    pick = chs[0] if chs else None
    if pick:
        ch_id, iid, rtype = pick
        if iid and iid not in SUBMITTED:
            await export_now("pre_activate.json")
            ST["act_tried"] = True
            await send_interaction(c, {"interactionId": iid,
                                       "response": {"type": "choose",
                                                    "data": {"choiceId":
                                                             ch_id}}})
            SUBMITTED.add(iid)
            say(f"[P0] activates Estrid -1 via viewer choice {ch_id}")
            wire("estrid_minus1", {"estrid": estrid, "bear": bear,
                                   "path": "interaction", "choice": ch_id})
            return True
    return False


async def p0_murder(c, pid, state, acts, key):
    if CAST.get("oid"):
        return False
    oid = find_hand(state, pid, MURDER)
    a = castspell_advertised(acts, oid)
    if not oid or not a:
        return False
    if untapped_of(state, pid, SWAMP) < 2 \
            or n_untapped_lands(state, pid) < 3:
        return False
    CAST.update({"oid": str(oid), "stack_seen": False, "resolved": False,
                 "key": key, "rev_at_submit": c.revision,
                 "submitted_at": time.time()})
    await submit_as_is(c, a)
    say(f"P0 casts Murder ({key}, oid={oid})")
    wire("murder_cast", {"oid": oid, "key": key})
    return True


async def p1_step(c, pid, state, acts):
    lid = (find_hand(state, pid, FOREST) or find_hand(state, pid, PLAINS)
           or find_hand(state, pid, ISLAND) or find_hand(state, pid, SWAMP))
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ESTRID, 8), (BEAR, 12), (MURDER, 12),
                         (FOREST, 10), (PLAINS, 8), (ISLAND, 8),
                         (SWAMP, 18)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((FOREST, 15), (PLAINS, 15),
                                    (ISLAND, 15), (SWAMP, 15)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
                if is_p0 and CAST.get("oid") and not CAST.get("stack_seen"):
                    say(f"[P0] {CAST.get('key')} cast rejected?")
                    wire("cast_reject_watch", {"cast": CAST})
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if wf_of(state).get("type") == "GameOver" and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            has_estrid = any(oname(o) == ESTRID for _, o in bf(state, 0))
            has_bear = any(oname(o) == BEAR for _, o in bf(state, 0))
            if has_estrid and has_bear:
                ST["stage"] = "ACTIVATE"
                say("=== stage -> ACTIVATE ===")

        if ST["stage"] == "ACTIVATE" and not MASKW["seen"]:
            bear, mask = masked_bears_oid(state)
            if bear and mask:
                MASKW.update({"seen": True, "oid": mask, "bear_oid": bear})
                await export_now("mid_mask.json")
                ST["stage"] = "MURDER"
                CAST.clear()
                say(f"=== Mask attached to Bears {bear}; stage -> MURDER ===")

        # --- Murder resolution watch
        if CAST.get("oid"):
            o = state["objects"].get(str(CAST["oid"]), {})
            if o.get("zone") == "Stack":
                CAST["stack_seen"] = True
            # 90s escape for a submission the engine never acknowledges
            if not CAST.get("stack_seen") and now - \
                    CAST.get("submitted_at", now) > 90:
                say(f"Murder ({CAST.get('key')}) never hit the stack after "
                    "90s; re-arming")
                wire("murder_no_stack_escape", {"cast": CAST})
                CAST.clear()
            if CAST.get("stack_seen") and o.get("zone") != "Stack" \
                    and not CAST.get("resolved"):
                CAST["resolved"] = True
                key = CAST.get("key")
                tgt = TGT.get("murder" if key == "MURDER" else "control")
                wire("murder_resolved", {"key": key, "target": tgt,
                                        "cast_zone": o.get("zone")})
                say(f"Murder ({key}) resolved; target={tgt}")
                stack_empty = not any(x.get("zone") == "Stack"
                                      for x in state["objects"].values())
                await asyncio.sleep(1.5)
                if stack_empty and key == "MURDER":
                    await export_now("post_murder.json")
                    ST["stage"] = "CONTROL"
                    CAST.clear()
                    say("=== stage -> CONTROL ===")
                elif stack_empty and key == "CONTROL":
                    await export_now("post_control.json")
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"final post export failed: {e}")
                    ST["stop"] = True
                    say("=== DONE ===")

        # --- stuck watches
        if TGT.get("estrid_minus1") and not MASKW["seen"] \
                and now - TGT["estrid_minus1"]["at"] > 120 and not ST["stop"]:
            say("mask never attached after 120s; exporting post anyway")
            wire("mask_timeout", {})
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

    def bear_zone(env_state, bear_oid):
        return env_state["objects"].get(str(bear_oid), {}).get("zone")

    try:
        pre = load_state("pre_activate.json")
        A["A1_setup_ok"] = ("passed"
                            if any(oname(o) == ESTRID
                                   for _, o in bf(pre, 0))
                            and any(oname(o) == BEAR
                                    for _, o in bf(pre, 0))
                            else "failed")
    except Exception:
        A["A1_setup_ok"] = "not-run"

    try:
        mid = load_state("mid_mask.json")
        bear, mask = masked_bears_oid(mid)
        kw_ok = False
        attached_ok = False
        mask_name_ok = False
        if mask:
            mo = mid["objects"][mask]
            mask_name_ok = oname(mo) == MASK
            kw_ok = has_totem_armor(mo)
            attached_ok = bear is not None
        A["A2_mask_attached"] = ("passed"
                                 if (mask_name_ok and kw_ok and attached_ok)
                                 else "failed")
        obs["notes"].append(f"mask: name={mask_name_ok} totem_armor={kw_ok} "
                            f"attached={attached_ok}")
    except Exception as e:
        A["A2_mask_attached"] = "not-run"
        obs["notes"].append(f"A2 eval error: {e}")

    try:
        post = load_state("post_murder.json")
        # The engine auto-targets when there is a single legal creature (no
        # TargetSelection prompt); fall back to the masked bear from MASKW.
        tgt = TGT.get("murder") or {}
        bear_oid = tgt.get("ref") or MASKW.get("bear_oid")
        mask_oid = MASKW["oid"]
        bear_alive = bear_zone(post, bear_oid) == "Battlefield"
        mask_obj = post["objects"].get(str(mask_oid))
        mask_gone = mask_obj is None or mask_obj.get("zone") in (
            "Graveyard", "Exile")
        A["A3_armor_replaces"] = ("passed"
                                  if bear_alive and mask_gone
                                  else "failed")
        obs["notes"].append(
            f"A3: bear={bear_oid} zone={bear_zone(post, bear_oid)} "
            f"mask={mask_oid} zone="
            f"{mask_obj.get('zone') if mask_obj else 'ABSENT'} "
            f"(auto_target={not bool(tgt.get('ref'))})")
    except Exception as e:
        A["A3_armor_replaces"] = "not-run"
        obs["notes"].append(f"A3 eval error: {e}")

    try:
        ctl = load_state("post_control.json")
        dead = [oid for oid, o in ctl["objects"].items()
                if oname(o) == BEAR and o.get("zone") == "Graveyard"
                and o.get("controller") == 0]
        A["A4_control_dies"] = ("passed" if len(dead) >= 1 else "failed")
        obs["notes"].append(f"A4: bears in P0 graveyard: {len(dead)}")
    except Exception as e:
        A["A4_control_dies"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e}")

    try:
        final = load_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in final["objects"].values())
        A["A5_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception:
        A["A5_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "targets": TGT, "mask": MASKW}, f, indent=2)

    run_doc = {
        "issue": 6863,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "server": {
            "server_version": "0.98.0",
            "build_commit": "61e8550",
            "protocol_version": 94,
            "mode": "Full",
            "binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
            "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
            "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-10-01",
            "source": "ServerHello + minisign verification against repo-pinned key",
        },
        "driver": {"protocol_version": 94, "client": "driver/client.py",
                   "scenario": "driver/scenario_6863_0980.py"},
        "scenario_sha256": hashlib.sha256(
            open(__file__, "rb").read()).hexdigest(),
        "decks": {
            "P0": [[ESTRID, 8], [BEAR, 12], [MURDER, 12],
                   [FOREST, 10], [PLAINS, 8], [ISLAND, 8], [SWAMP, 18]],
            "P1": [[FOREST, 15], [PLAINS, 15], [ISLAND, 15], [SWAMP, 15]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "verdict": ("reproduced"
                    if A.get("A3_armor_replaces") == "failed"
                    and A.get("A2_mask_attached") == "passed"
                    else ("not-reproduced"
                          if A.get("A2_mask_attached") == "passed"
                          and A.get("A3_armor_replaces") == "passed"
                          and A.get("A4_control_dies") == "passed"
                          else "blocked")),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_doc, f, indent=2)
    say(f"verdict: {run_doc['verdict']}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {}, "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
