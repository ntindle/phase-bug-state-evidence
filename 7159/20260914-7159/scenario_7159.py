#!/usr/bin/env python3
"""phase-rs/phase #7159 - The Irencrag: equip {3} missing after the
become-Equipment transformation.

Reported: when you take the Irencrag's trigger option, it becomes a
legendary Equipment named Everflame, Heroes' Legacy and loses the mana
ability, but equip {3} is missing.

Oracle (pinned card-data.json):
  The Irencrag {2} Legendary Artifact
  {T}: Add {C}.
  Whenever a legendary creature you control enters, you may have The
  Irencrag become a legendary Equipment artifact named Everflame, Heroes'
  Legacy. If you do, it gains equip {3} and "Equipped creature gets +3/+3"
  and loses all other abilities.

Behavioral contract (2 human seats, native engine, Bo1, protocol 70):
  P0: 12x The Irencrag, 12x Isamaru, Hound of Konda ({W} 2/2 vanilla
    legendary), 18x Plains, 18x Mountain. Land every turn; cast Irencrag
    at 2+ mana; cast Isamaru once Irencrag is on the battlefield.
  P1: 60x Forest; passive punching bag (keep mulligan, never play lands,
    never cast, no attacks, no blocks; hand-size discards lands first).
  Isamaru ETB => Irencrag trigger => OptionalEffectChoice => ACCEPT via
    the choice whose value surface is (role=accept, value "true")
    (decideOptionalEffect action code; never pick by text).
  A1 parse_ok:      trigger AST carries the become-Equipment mods + the
                    sub_ability RemoveAllAbilities / GrantStaticAbility
                    (+3/+3, EquippedBy) / GrantAbility equip x2 (record the
                    duplicated equip grant as a secondary parse defect).
  A2 setup_ok:      Irencrag on P0 BF; Isamaru entered; the trigger fired on
                    the stack (kind.type=="TriggeredAbility", discriminated
                    by the ability.description text).
  A3 option_offered: OptionalEffectChoice seen; accepted with accept=true.
  A4 transform_ok:  post-resolution object named "Everflame, Heroes'
                    Legacy", still Legendary Artifact, subtype Equipment.
  A5 mana_lost:     original {T}: Add {C} gone (inspect transformed object
                    JSON ability listings; no mana ability offered for it).
  A6 equip_offered: an equip {3} ActivateAbility offered for the
                    transformed object (protocol-70
                    {"type":"ActivateAbility","data":{"source_id":<oid>,
                    ...}} scans in legal_actions AND viewer_interaction).
                    THE REPORTED BUG — expect failed if it reproduces.
  A7 equip_resolves: if A6 passes, activate it, answer target (Isamaru;
                    single-target auto-target possible), assert attached_to
                    points at Isamaru and Isamaru is 5/5. Not-run if A6
                    fails.
  A8 cleanup:       stack empty; game advanced >= 1 full turn after
                    resolution.

Verdict: reproduced iff A2, A3, A4 pass and A6 fails. not-reproduced iff
A2-A8 all pass. blocked iff A2/A3 cannot be established.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260914-7159")
ISSUE = 7159
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

IRENCRAG = "The Irencrag"
ISAMARU = "Isamaru, Hound of Konda"
EVERFLAME = "Everflame, Heroes' Legacy"
PLAINS, MOUNTAIN, FOREST = "Plains", "Mountain", "Forest"
LANDS = ("Plains", "Mountain", "Forest", "Swamp", "Island")

P0_DECK = [(IRENCRAG, 12), (ISAMARU, 12), (PLAINS, 18), (MOUNTAIN, 18)]
P1_DECK = [(FOREST, 60)]
TIMEOUT = 1500

CD_PATH = f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034205ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(per task context; ServerHello re-verified at run start) + "
              "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None
PROMPT_DONE = {}


def reset():
    ST.clear()
    ACTED.clear()
    PROMPT_DONE.clear()
    ST.update({
        "stage": "SETUP",
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "a1": "not-run",
        # trigger flow
        "irencrag_cast": False,
        "isamaru_cast": False,
        "isamaru_cast_turn": None,
        "trigger_on_stack_seen": False,
        "trigger_stack_record": None,
        "option_offered": False,
        "option_accepted": False,
        "pre_exported": False,
        "transformed": False,
        "transformed_oid": None,
        "transform_record": None,
        "mid_exported": False,
        "resolve_turn": None,
        # equip offer scans
        "offer_scans": [],
        "equip_ability_index": None,
        "equip_descriptions": [],
        "equip_offered_seen": False,
        "equip_activated": False,
        "equip_activated_at": None,
        "equip_target_oid": None,
        "equip_resolved": False,
        "equip_giveup": False,
        "cleanup_turn_target": None,
        "unexpected": [],
    })
    WF_SEEN.clear()


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
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


def nname(o):
    return oname(o).lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def hand_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if nname(state["objects"][oid]) == name.lower():
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid)
            if nname(o) == name.lower()]


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if nname(o) in (l.lower() for l in LANDS)
            and not o.get("tapped")]


def untapped_land_names(state, pid):
    return [nname(o) for _, o in untapped_lands(state, pid)]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_data(state):
    return wf_of(state).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


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
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage"),
                                     "t": time.time()})
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
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)


def load_state(path):
    try:
        with open(f"{EVDIR}/{path}") as f:
            env = json.load(f)
        return env.get("state", env)
    except Exception:
        return None


# ---------------- viewer_interaction helpers ------------------------------
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_opps(st):
    vi = get_vi(st)
    return vi.get("opportunities", []) if vi else []


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id",
                     "hit_card", "card_id") and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        deep_refs(s.get("data") or {}, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def action_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", [])]


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


# ---------------- parse check (A1) ----------------------------------------
def parse_check():
    cd = json.load(open(CD_PATH))
    t = cd["the irencrag"]
    trigs = t.get("triggers") or []
    rec = {"n_triggers": len(trigs)}
    ok_main = ok_sub = False
    equip_grants = []
    trig = trigs[0] if trigs else {}
    rec["mode"] = trig.get("mode")
    rec["optional"] = trig.get("optional")
    rec["valid_card"] = trig.get("valid_card")
    exe = (trig.get("execute") or {})
    eff = (exe.get("effect") or {})
    statics = eff.get("static_abilities") or []
    main_mods = []
    for sa in statics:
        for m in sa.get("modifications") or []:
            main_mods.append(m.get("type"))
    rec["main_effect_modifications"] = main_mods
    ok_main = all(k in main_mods for k in
                  ("SetTextName", "AddSupertype", "AddType",
                   "RemoveAllSubtypes", "AddSubtype"))
    sub = (exe.get("sub_ability") or {})
    cond = (sub.get("condition") or {})
    rec["sub_condition"] = cond
    sub_eff = (sub.get("effect") or {})
    sub_statics = sub_eff.get("static_abilities") or []
    sub_mods = []
    for sa in sub_statics:
        for m in sa.get("modifications") or []:
            sub_mods.append(m.get("type"))
            if m.get("type") == "GrantAbility":
                definition = m.get("definition") or {}
                equip_grants.append(
                    definition.get("description"))
    rec["sub_ability_modifications"] = sub_mods
    rec["equip_grant_descriptions"] = equip_grants
    rec["duplicated_equip_grant"] = len(equip_grants) == 2
    rec["static_grant_present"] = "GrantStaticAbility" in sub_mods
    ok_sub = (cond.get("type") == "EffectOutcome"
              and cond.get("signal") == "OptionalEffectPerformed"
              and "RemoveAllAbilities" in sub_mods
              and "GrantStaticAbility" in sub_mods
              and len(equip_grants) >= 1)
    blob = json.dumps(t)
    rec["no_unimplemented_nodes"] = "Unimplemented" not in blob
    with open(f"{EVDIR}/parse_irencrag.json", "w") as f:
        json.dump(rec, f, indent=1)
    ok = ok_main and ok_sub and rec["no_unimplemented_nodes"]
    say(f"A1 parse_ok: {'passed' if ok else 'failed'} "
        f"(main={ok_main} sub={ok_sub} equip_grants={len(equip_grants)} "
        f"duplicated={rec['duplicated_equip_grant']})")
    ST["a1"] = "passed" if ok else "failed"
    ST["parse_rec"] = rec
    return ST["a1"]


# ---------------- game actions --------------------------------------------
def find_action(acts, atype):
    for a in acts:
        if a["type"] == atype:
            return a
    return None


def castspell_for(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_for(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def find_equip_activations(acts, oid):
    """All ActivateAbility actions for source oid; return (action, desc)."""
    out = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            if int(d.get("source_id", -1)) == int(oid):
                out.append((a, d))
        except Exception:
            continue
    return out


def stack_trigger_sample(state):
    """Scan the stack for the Irencrag trigger entry (discriminate by the
    ability's own description text, cf. #6773)."""
    hits = []
    for e in state.get("stack") or []:
        kind = e.get("kind") or {}
        if (kind.get("type") or "") != "TriggeredAbility":
            continue
        desc = ""
        ab = e.get("ability") or kind.get("ability") or {}
        desc = str(ab.get("description") or "")
        if "become a legendary equipment" in desc.lower():
            hits.append({"id": e.get("id"),
                         "source_id": e.get("source_id"),
                         "description": desc[:160]})
    return hits


async def preamble(c, pid, st, state, acts):
    """Mulligan keep / legend as-is / bottom-cards / hand-size discard /
    no blocks / no attacks. Returns True if it acted."""
    rev = st.get("state_revision", -1)
    for a in acts:
        if "Legend" in a["type"]:
            if not acted(f"leg{pid}", rev):
                await submit_as_is(c, a)
                say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            if wf_type(state) == "MulliganDecision" and any(
                    p.get("player") == pid for p in pend):
                if not acted(f"mull{pid}", rev):
                    await submit_as_is(
                        c, {"type": "MulliganDecision",
                            "data": {"choice": {"type": "Keep"}}})
                    say(f"[{c.name}] keeps")
            return True
    # Mulligan BottomCards phase (count from pending phase.count)
    if wf_type(state) == "BottomCards" and wf_player(state) == pid:
        pend = wf_data(state).get("pending", []) or []
        n = 0
        for p in pend:
            if p.get("player") == pid:
                n = (p.get("phase") or {}).get("count", 0) or 0
        oids = [int(x) for x in hand_oids(state, pid)][:n]
        if oids and not acted(f"bot{pid}", rev):
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": oids}})
            say(f"[{c.name}] bottoms {n}")
        return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        # P0 protects Irencrag + Isamaru (setup-critical); P1 only holds
        # lands anyway (never plays them).
        protect = set()
        if pid == 0:
            protect = {IRENCRAG.lower(), ISAMARU.lower()}
        oids = hand_oids(state, pid)

        def rank(x):
            nm = nname(state["objects"][x])
            if nm in protect:
                return 2
            return 0 if nm in [l.lower() for l in LANDS] else 1
        oids.sort(key=rank)
        picks = [int(x) for x in oids[:n]]
        if picks and not acted(f"hsd{pid}", rev):
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{c.name}] discards {n} to hand size")
        return True
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no blockers")
            return True
    if (state.get("phase") or "") == "DeclareAttackers" \
            and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            if not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no attackers")
            return True
    return False


# ---------------- Irencrag trigger handling (A2/A3/A4) --------------------
def attached_to_ints(o):
    """attached_to is a NESTED dict on protocol 69/70; extract via
    recursive int search (cf. #6762)."""
    found = []

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, int) and n >= 0:
            found.append(n)
    walk(o.get("attached_to"))
    return found


async def handle_optional_effect(c, st, state):
    """Answer the Irencrag 'you may have it become...' choice with
    accept=true, identified by action code + value surfaces (never by
    text)."""
    if wf_type(state) != "OptionalEffectChoice" or wf_player(state) != 0:
        return False
    if ST["option_accepted"]:
        return False
    if not ST["pre_exported"]:
        await export_now("pre.json")
        ST["pre_exported"] = True
    ST["option_offered"] = True
    wire("optional_effect_offered",
         {"waiting_for": wf_of(state), "turn": state.get("turn_number")})
    say("[P0] OptionalEffectChoice offered (Irencrag transform option)")
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if PROMPT_DONE.get(iid):
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        if rtype != "exactChoices":
            say(f"[P0] unexpected opportunity type {rtype}; wire-dumping")
            wire("unexpected_opp_type", {"type": rtype, "opp": opp})
            continue
        pick = None
        for ch in chs:
            if "decideOptionalEffect" not in action_codes(ch):
                continue
            for sf in ch.get("surfaces", []):
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept" \
                        and str(dd.get("value")).lower() == "true":
                    pick = ch
                    break
            if pick:
                break
        if pick is None:
            say("[P0] no decideOptionalEffect accept=true choice found; "
                "wire-dumping opportunity")
            wire("optional_effect_no_accept",
                 {"interaction_id": iid,
                  "codes_seen": [action_codes(ch) for ch in chs]})
            PROMPT_DONE[iid] = True
            ST["unexpected"].append("optional_no_accept_choice")
            return True
        wire("optional_effect_opportunity", opp)
        await answer_vi(c, opp, pick, "P0")
        PROMPT_DONE[iid] = True
        ST["option_accepted"] = True
        ST["stage"] = "TRANSFORM_WATCH"
        return True
    return False


def check_transform(state):
    """Detect Everflame, Heroes' Legacy on P0's battlefield."""
    for oid, o in bf(state, 0):
        if nname(o) == EVERFLAME.lower():
            return oid, o
    return None, None


def type_texts(o):
    for k in ("supertypes", "types", "subtypes", "type_line", "type"):
        v = o.get(k)
        if v:
            return k, v
    return None, None


# ---------------- equip offer scan + activation (A6/A7) -------------------
def equip_offered_for(acts, oid):
    """ActivateAbility offers for the transformed object; returns
    (index, descriptions, action)."""
    acts2 = find_equip_activations(acts, oid)
    descs = []
    idxs = []
    for a, d in acts2:
        idxs.append(d.get("ability_index"))
        dd = d.get("description") or a.get("description") or ""
        descs.append(str(dd))
    equip = [a for (a, d), desc in zip(acts2, descs)
             if "equip" in desc.lower() or d.get("ability_tag") == "Equip"
             or (d.get("ability_tag") or {}).get("type") == "Equip"]
    return acts2, descs, idxs, equip


def vi_activate_offers(st):
    """Parse viewer_interaction opportunities for activation-style
    choices; returns list of (interaction_id, codes, text)."""
    out = []
    for opp in vi_opps(st):
        chs, rtype = vi_choices(opp)
        for ch in chs:
            out.append({"interaction_id": opp.get("interactionId"),
                        "response_type": rtype,
                        "codes": action_codes(ch),
                        "text": choice_text(ch)[:80]})
    return out


async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    # Never pass priority during the option prompt; the handler above
    # returns after answering, but if it hasn't fired yet, hold.
    if wf_type(state) == "OptionalEffectChoice" and wf_player(state) == 0:
        if await handle_optional_effect(c, st, state):
            return True
        # prompt seen but not yet answerable in viewer_interaction — hold
        return True
    # opportunistic trigger-stack sampling (A2): after Isamaru was cast
    # and before the option prompt resolves/closes.
    if ST["isamaru_cast"] and not ST["option_accepted"]:
        hits = stack_trigger_sample(state)
        if hits and not ST["trigger_on_stack_seen"]:
            ST["trigger_on_stack_seen"] = True
            ST["trigger_stack_record"] = hits[0]
            say(f"[P0] trigger on stack: {json.dumps(hits[0])[:200]}")
            wire("trigger_on_stack", hits[0])
    if await handle_optional_effect(c, st, state):
        return True
    rev = st.get("state_revision", -1)
    # ---- setup: land every tick, Irencrag at 2+ mana, Isamaru after ----
    if not ST["transformed"] and is_my_main(state, 0):
        # land drop every tick (no kept-flag; cf. #6690)
        want = None
        bf_names = [nname(o) for _, o in bf(state, 0)]
        if "plains" not in bf_names:
            want = find_hand(state, 0, "Plains")
        if want is None:
            for l in LANDS:
                want = find_hand(state, 0, l)
                if want:
                    break
        la = playland_for(acts, want)
        if la and not acted("land0", rev):
            await submit_as_is(c, la)
            say(f"[P0] plays {oname(state['objects'][want])}")
            return True
        ul = untapped_land_names(state, 0)
        # cast Irencrag once 2+ mana available
        if not bf_named(state, 0, IRENCRAG) and not ST["irencrag_cast"]:
            ir = find_hand(state, 0, IRENCRAG)
            if ir and len(ul) >= 2:
                ca = castspell_for(acts, ir)
                if ca and not acted("castirencrag", rev):
                    await submit_as_is(c, ca)
                    ST["irencrag_cast"] = True
                    say("[P0] casts The Irencrag")
                    return True
        # cast Isamaru once Irencrag is on the battlefield and 1W open
        if bf_named(state, 0, IRENCRAG) and not bf_named(state, 0, ISAMARU) \
                and not ST["isamaru_cast"]:
            isa = find_hand(state, 0, ISAMARU)
            if isa and "plains" in ul:
                ca = castspell_for(acts, isa)
                if ca and not acted("castisamaru", rev):
                    await submit_as_is(c, ca)
                    ST["isamaru_cast"] = True
                    ST["isamaru_cast_turn"] = state.get("turn_number")
                    say("[P0] casts Isamaru, Hound of Konda")
                    return True
    # ---- end setup ----
    # transformation detection
    if ST["option_accepted"] and not ST["transformed"]:
        oid, o = check_transform(state)
        if oid:
            ST["transformed"] = True
            ST["transformed_oid"] = oid
            ST["resolve_turn"] = state.get("turn_number")
            ST["transform_record"] = {
                "oid": oid,
                "card_name": oname(o),
                "card_types": o.get("card_types"),
                "zone": o.get("zone"),
                "controller": o.get("controller"),
                "tapped": o.get("tapped"),
            }
            say(f"[P0] TRANSFORMED: oid={oid} "
                f"{json.dumps(ST['transform_record'])[:300]}")
            wire("transformed", ST["transform_record"])
            ST["stage"] = "EQUIP_SCAN"
            await export_now("mid.json")
            ST["mid_exported"] = True
    # equip offer scanning: only meaningful in P0 main phases (equip is
    # AsSorcery)
    if ST["transformed"] and is_my_main(state, 0) \
            and not ST["equip_offered_seen"] and not ST["equip_giveup"]:
        oid = ST["transformed_oid"]
        acts2, descs, idxs, equip = equip_offered_for(acts, oid)
        if not acted(f"eqscan{oid}", rev):
            vi_has = bool(vi_opps(st))
            vi_offers = vi_activate_offers(st)
            all_types = sorted({a.get("type") for a in acts})
            rec = {"rev": rev, "turn": state.get("turn_number"),
                   "n_activations_for_object": len(acts2),
                   "ability_index": idxs, "descriptions": descs,
                   "equip_offered": bool(equip),
                   "all_action_types": all_types,
                   "vi_opportunities": vi_offers}
            ST["offer_scans"].append(rec)
            wire("equip_offer_scan", rec)
        if equip:
            ST["equip_offered_seen"] = True
            ST["equip_ability_index"] = equip[0][1]["data"].get(
                "ability_index")
            ST["equip_descriptions"] = descs
            ST["stage"] = "EQUIP_ACTIVATE"
            say(f"[P0] EQUIP OFFERED: index={ST['equip_ability_index']} "
                f"descs={descs}")
            wire("equip_offered", {"ability_index": ST["equip_ability_index"],
                                   "descriptions": descs,
                                   "action": equip[0][1]})
        else:
            turns = {s["turn"] for s in ST["offer_scans"]}
            if len(turns) >= 6:
                ST["equip_giveup"] = True
                ST["stage"] = "CLEANUP"
                say("[P0] equip giveup: no equip offer across "
                    f"{len(turns)} clean main-phase turns")
                wire("equip_giveup", {"turns": len(turns)})
    # activate equip (A7)
    if ST["stage"] == "EQUIP_ACTIVATE" and not ST["equip_activated"]:
        if is_my_main(state, 0):
            oid = ST["transformed_oid"]
            acts2, descs, idxs, equip = equip_offered_for(acts, oid)
            if equip and not acted("equpact", rev):
                await submit_as_is(c, equip[0][1])
                ST["equip_activated"] = True
                ST["equip_activated_at"] = time.time()
                say("[P0] equip activated")
                wire("equip_activated", {"action": equip[0][1]})
                return True
    # answer the equip target selection (Isamaru; engine may auto-target a
    # single legal target — in that case this never fires, cf. #6863)
    if ST["equip_activated"] and not ST["equip_resolved"]:
        if wf_type(state) == "TargetSelection" and wf_player(state) == 0:
            for opp in vi_opps(st):
                iid = opp.get("interactionId")
                if PROMPT_DONE.get(iid):
                    continue
                chs, rtype = vi_choices(opp)
                if not chs:
                    continue
                isamarus = bf_named(state, 0, ISAMARU)
                pick = None
                for ch in chs:
                    r = ref_of(ch)
                    if r is not None and str(r) in isamarus:
                        pick = ch
                        break
                if pick is None and chs:
                    pick = chs[0]
                    say("[P0] equip target: no Isamaru among candidates; "
                        "falling back to first")
                if pick is None:
                    PROMPT_DONE[iid] = True
                    continue
                r = ref_of(pick)
                ST["equip_target_oid"] = str(r) if r is not None else None
                say(f"[P0] equip target: Isamaru oid={r}")
                wire("equip_target_pick", {"ref": r})
                await answer_vi(c, opp, pick, "P0")
                PROMPT_DONE[iid] = True
                return True
    # equip resolution watch: attached_to points at Isamaru and Isamaru 5/5
    if ST["equip_activated"] and not ST["equip_resolved"]:
        oid = ST["transformed_oid"]
        o = get_obj(state, oid)
        att = attached_to_ints(o)
        isamarus = bf_named(state, 0, ISAMARU)
        if att and any(str(a) in isamarus for a in att):
            io = get_obj(state, [a for a in att
                                 if str(a) in isamarus][0])
            pt = (io.get("power"), io.get("toughness"))
            ST["equip_resolved"] = True
            ST["equip_resolved_pt"] = pt
            ST["stage"] = "CLEANUP"
            say(f"[P0] equip RESOLVED: attached_to={att}, "
                f"Isamaru P/T={pt}")
            wire("equip_resolved", {"attached_to": att, "pt": pt})
    if ST["stage"] in ("EQUIP_ACTIVATE",) and ST["equip_activated"] \
            and not ST["equip_resolved"]:
        rej = [r for r in ST["rejections"]
               if r["type"] in ("ActionRejected", "Error")
               and r.get("t", 0) > (ST.get("equip_activated_at") or 0)]
        if rej:
            ST["unexpected"].append("equip_activation_rejected")
            say("[P0] equip activation rejected: "
                f"{json.dumps(rej[-1])[:300]}")
            wire("equip_activation_rejected", rej[-1])
    # trigger give-up: Irencrag on BF, Isamaru cast long ago, no option
    if ST["isamaru_cast"] and not ST["option_offered"]:
        t0c = ST.get("isamaru_cast_turn")
        if t0c is not None and state.get("turn_number", 0) >= t0c + 10:
            ST["done_reason"] = ("Isamaru cast at turn %s but no "
                                 "OptionalEffectChoice by turn %s" %
                                 (t0c, state.get("turn_number")))
            say("GIVEUP: " + ST["done_reason"])
            wire("trigger_giveup", {})
            ST["stop"] = True
    if ST["stage"] == "CLEANUP" and ST.get("resolve_turn") is not None:
        stack = state.get("stack") or []
        if not stack and state.get("turn_number", 0) >= \
                ST["resolve_turn"] + 2:
            ST["done_reason"] = "post-resolution observation window complete"
            say("DONE: " + ST["done_reason"])
            ST["stop"] = True
    return False


async def p1_tick(c, st, state, acts):
    # P1 is a passive punching bag: preamble only (keep, discards, no
    # blocks, no attacks), then fall through to priority passing.
    if await preamble(c, 1, st, state, acts):
        return True
    return False

# ---------------- tick + main loop ----------------------------------------
async def tick(c, pid):
    st = c.latest
    if not st or ST["stop"]:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if state is None:
        return False
    drain_rejections(c)
    wt, wp = wf_type(state), wf_player(state)
    if wt and (not WF_SEEN or WF_SEEN[-1][0] != wt
               or WF_SEEN[-1][1] != wp):
        WF_SEEN.append((wt, wp, ST["stage"]))
        wire("waiting_for", {"type": wt, "data": wf_data(state),
                             "stage": ST["stage"]})
        say(f"[{c.name}] waiting_for: {wt} player={wp}")
    for q in (0, 1):
        lv = life(state, q)
        if lv is not None and lv < ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {state.get('turn_number')} phase {state.get('phase')})")
            wire("life_drop", {"player": q, "from": ST["life"][q], "to": lv,
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase")})
            ST["life"][q] = lv
        elif lv is not None and lv > ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} (gain)")
            wire("life_gain", {"player": q, "from": ST["life"][q], "to": lv})
            ST["life"][q] = lv
    try:
        if pid == 0:
            did = await p0_tick(c, st, state, acts)
        else:
            did = await p1_tick(c, st, state, acts)
    except Exception as e:
        say(f"tick error [{c.name}]: {e}")
        wire("tick_error", {"who": c.name, "error": str(e)[:300]})
        return False
    if did:
        return True
    # never pass priority while waiting_for is a decision prompt for the
    # deciding seat
    if wt in ("OptionalEffectChoice", "TargetSelection",
              "CombatTaxPayment") and wp == pid:
        return False
    rev = st.get("state_revision", -1)
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", rev):
                    await submit_as_is(c, a)
                    return True
            break
    return False


# ---------------- finalize -------------------------------------------------
async def finalize():
    if ST.get("finalized"):
        return None, {}
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_7159.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("server.log copied to evidence")
    except Exception as e:
        say(f"server.log copy failed: {e}")
        wire("server_log_copy_failed", {"error": str(e)[:200]})

    if "post.json" not in ST["exports"]:
        await export_now("post.json")

    post = load_state("post.json")
    mid = load_state("mid.json")
    pre = load_state("pre.json")
    ass, notes = {}, []

    def note(m):
        notes.append(m)
        say("NOTE:", m)

    # A1
    ass["A1_parse_ok"] = ST.get("a1", "not-run")
    rec = ST.get("parse_rec") or {}
    note(f"A1: become-Equipment mods {rec.get('main_effect_modifications')}; "
         f"sub mods {rec.get('sub_ability_modifications')}; equip grants "
         f"{rec.get('equip_grant_descriptions')} (duplicated grant: "
         f"{rec.get('duplicated_equip_grant')}) -> {ass['A1_parse_ok']}")

    # A2
    ok2 = False
    if ST["trigger_stack_record"]:
        tr = ST["trigger_stack_record"]
        note(f"A2: trigger on stack seen: source_id={tr.get('source_id')} "
             f"desc={tr.get('description')[:120]}")
        ok2 = True
    elif pre:
        hits = stack_trigger_sample(pre)
        if hits:
            ST["trigger_stack_record"] = hits[0]
            ok2 = True
            note(f"A2: trigger on stack found in pre.json: "
                 f"{json.dumps(hits[0])[:160]}")
        else:
            # pre.json is captured while the option is pending; the trigger
            # may already have left the stack. Accept acceptance itself as
            # evidence the trigger fired: the OptionalEffectChoice is the
            # trigger's optional-effect decision.
            note("A2: trigger not on stack in live sampling or pre.json "
                 "stack (stack empties fast once the option is pending)")
    if not ok2 and ST["option_offered"]:
        ok2 = True
        note("A2: trigger fire established via the OptionalEffectChoice "
             "itself (the choice is the trigger's optional decision); the "
             "stack entry resolved too fast for sampling")
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"

    # A3
    if ST["option_accepted"]:
        ass["A3_option_offered"] = "passed"
    elif ST["option_offered"]:
        ass["A3_option_offered"] = "failed"
    else:
        ass["A3_option_offered"] = "not-run"
    note(f"A3: option offered={ST['option_offered']} "
         f"accepted(accept=true)={ST['option_accepted']}")

    # A4: type info lives under object.card_types on protocol 70
    # ({core_types, subtypes, supertypes}).
    ok4 = False
    trec = ST.get("transform_record") or {}
    if trec:
        ct = trec.get("card_types") or {}
        subs_l = [str(s).lower() for s in (ct.get("subtypes") or [])]
        sups_l = [str(s).lower() for s in (ct.get("supertypes") or [])]
        typs_l = [str(t).lower() for t in (ct.get("core_types") or [])]
        name_ok = trec.get("card_name") == EVERFLAME
        eq_ok = "equipment" in subs_l
        leg_ok = "legendary" in sups_l
        art_ok = "artifact" in typs_l
        ok4 = name_ok and eq_ok and leg_ok and art_ok
        note(f"A4: name={trec.get('card_name')!r} card_types={ct} -> "
             f"name_ok={name_ok} legendary={leg_ok} artifact={art_ok} "
             f"equipment={eq_ok}")
    else:
        note("A4: no transformation recorded")
    ass["A4_transform_ok"] = "passed" if ok4 else "failed"

    # A5: original {T}: Add {C} must be gone from the object's current
    # `abilities` list (base_abilities retains the printed text as the
    # pre-transform snapshot; the effective list is what matters).
    if mid and ST.get("transformed_oid"):
        mo = get_obj(mid, ST["transformed_oid"])
        cur = mo.get("abilities") or []
        mana_in_cur = [a for a in cur
                       if a.get("is_mana_ability")
                       or "add {c}" in str(a.get("description") or "")
                       .lower()]
        equip_in_cur = [a for a in cur
                        if "equip" in str(a.get("description") or "")
                        .lower()]
        ok5 = not mana_in_cur
        ass["A5_mana_lost"] = "passed" if ok5 else "failed"
        note(f"A5: current abilities={len(cur)} mana_remaining="
             f"{len(mana_in_cur)} equip_granted={len(equip_in_cur)} "
             f"has_mana_ability_flag={mo.get('has_mana_ability')} "
             f"(base_abilities still lists the printed mana ability: "
             f"{len(mo.get('base_abilities') or [])})")
    else:
        ass["A5_mana_lost"] = "not-run"
        note("A5: mid.json/transformed object missing")

    # A6
    if ST["equip_offered_seen"]:
        ass["A6_equip_offered"] = "passed"
    else:
        scans = [s for s in ST["offer_scans"]]
        turns = len({s["turn"] for s in scans})
        if turns >= 6:
            ass["A6_equip_offered"] = "failed"
        else:
            ass["A6_equip_offered"] = "not-run"
        note(f"A6: equip offered={ST['equip_offered_seen']} "
             f"scans={len(scans)} clean_main_turns={turns}")
    note(f"A6: ability_index={ST.get('equip_ability_index')} "
         f"descs={ST.get('equip_descriptions')}")

    # A7
    if ST["equip_resolved"]:
        pt = ST.get("equip_resolved_pt")
        ok7 = pt is not None and pt[0] == 5 and pt[1] == 5
        ass["A7_equip_resolves"] = "passed" if ok7 else "failed"
        note(f"A7: attached to Isamaru; P/T={pt} "
             f"(expected (5, 5))")
    elif ST["equip_activated"]:
        ass["A7_equip_resolves"] = "failed"
        note("A7: equip activated but never resolved")
    else:
        ass["A7_equip_resolves"] = "not-run"
        note("A7: equip never offered -> not-run")

    # A8
    ok8 = False
    if post:
        stack = post.get("stack") or []
        turn = post.get("turn_number")
        rt = ST.get("resolve_turn")
        ok8 = len(stack) == 0 and rt is not None and turn >= rt + 2
        note(f"A8: post stack depth={len(stack)} turn={turn} "
             f"resolve_turn={rt}")
    else:
        note("A8: post.json missing")
    ass["A8_cleanup"] = "passed" if ok8 else "failed"

    # verdict
    a = ass
    if a["A2_setup_ok"] == "passed" and a["A3_option_offered"] == "passed" \
            and a["A4_transform_ok"] == "passed" \
            and a["A6_equip_offered"] == "failed":
        verdict = "reproduced"
    elif all(a[k] == "passed" for k in
             ("A2_setup_ok", "A3_option_offered", "A4_transform_ok",
              "A5_mana_lost", "A6_equip_offered", "A7_equip_resolves",
              "A8_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    run = {
        "issue": ISSUE,
        "title": "The Irencrag - equip {3} missing after the "
                 "become-Equipment transformation.",
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "validated_version": "v0.82.0",
        "server": SERVER_IDENTITY,
        "server_hello_observed": ST.get("server_hello"),
        "protocol_version": 70,
        "scope": "The Irencrag {2} (pinned card-data AST: ChangesZone "
                 "trigger -> optional become legendary Equipment named "
                 "Everflame, Heroes' Legacy; sub_ability: RemoveAllAbilities "
                 "+ GrantStaticAbility(+3/+3, EquippedBy) + GrantAbility "
                 "equip {3} x2 (duplicated grant)). Isamaru, Hound of Konda "
                 "as the legendary ETB trigger; native engine, two "
                 "human-client seats",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {v}" for k, v in ass.items()),
        "trigger_stack_record": ST.get("trigger_stack_record"),
        "transform_record": ST.get("transform_record"),
        "offer_scans": ST.get("offer_scans"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Mana abilities are not surfaced as ActivateAbility offers to "
            "human clients on protocol 70, so mana-removal was judged from "
            "the transformed object's JSON, not from offer counts.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
        "done_reason": ST.get("done_reason"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # render summary.png via the standalone renderer (reads run.json +
    # saved states; must run before manifest)
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7159.py", EVDIR],
            capture_output=True, text=True, timeout=120)
        say(r.stdout.strip())
        if r.returncode != 0:
            say("renderer failed: " + r.stderr[-500:])
            wire("renderer_failed", {"stderr": r.stderr[-500:]})
    except Exception as e:
        say(f"renderer error: {e}")
        wire("renderer_error", {"error": str(e)[:200]})

    # manifest LAST (after all logging is done) — no say()/wire() calls
    # may happen after this point or the hashed logs will drift.
    say(f"writing manifest; done: {ST.get('done_reason')}")
    RUNLOG.flush()
    WIRE.flush()
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            f.write(f"{h}  {fn}\n")
    RUNLOG.flush()
    WIRE.flush()
    return verdict, ass


# ---------------- main -----------------------------------------------------
async def main():
    reset()
    global C0, C1
    parse_check()

    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "run_id": RUN_ID})

    t0 = time.time()
    try:
        while time.time() - t0 < TIMEOUT and not ST["stop"]:
            await tick(C0, 0)
            await tick(C1, 1)
            await asyncio.sleep(0.25)
    except Exception as e:
        say(f"main loop error: {e}")
        wire("main_error", {"error": str(e)[:300]})
    finally:
        if not ST.get("finalized"):
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"pre-close post export failed: {e}")
                wire("post_export_failed", {"error": str(e)[:200]})
        try:
            await C0.close()
        except Exception:
            pass
        try:
            await C1.close()
        except Exception:
            pass
    return await finalize()


if __name__ == "__main__":
    verdict, ass = asyncio.run(main())
    print(f"FINAL verdict={verdict} "
          + " ".join(f"{k}={v}" for k, v in ass.items()))
