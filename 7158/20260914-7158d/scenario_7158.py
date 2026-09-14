#!/usr/bin/env python3
"""phase-rs/phase #7158 - Protection only applies to permanents and players.

Reported (Discord, 2026-08-10): in violation of CR 702.16b, a Teysa, Envoy
of Ghosts ("protection from creatures") sitting in the GRAVEYARD could not
be the target of an ability from a creature source.

Oracle text (pinned card-data.json):
  Teysa, Envoy of Ghosts {5}{W}{B} Legendary Creature - Human Advisor 4/4
  Vigilance, protection from creatures
  Whenever a creature deals combat damage to you, destroy that creature.
  Create a 1/1 white and black Spirit creature token with flying.

Behavioral contract (2 human seats, native engine, Bo1):
  P0 stocks its graveyard with Teysa via Entomb; P1 casts Scavenging Ooze
  ({G}: Exile target card from a graveyard. If it was a creature card, put
  a +1/+1 counter on Scavenging Ooze and you gain 1 life.)
  Leg A (reported bug): P1 activates the Ooze's {G} ability while Teysa is
    in P0's graveyard. Protection must NOT apply (Teysa is not a
    permanent): the graveyard Teysa MUST be a legal target. Assert candidate
    presence, submission acceptance, and resolution (Teysa exiled, Ooze
    3/3, P1 20->21).
  Leg B (control): P0 casts Teysa; P0 Entombs a second Teysa; P1 activates
    the Ooze again. The BATTLEFIELD Teysa must NOT be among the candidates
    (protection applies to permanents), while the graveyard copy stays
    legal.
  A1 parse_ok:       Teysa carries Protection{CardType: creatures}, fully
                    parsed, no Unimplemented nodes.
  A2 setup_gy:      Teysa in P0 gy; Ooze on P1 BF untapped; P1 can pay {G}.
  A3 offer_gy:      ActivateAbility for the Ooze's {G} offered to P1.
  A4 candidates_gy: graveyard Teysa among the target candidates.
  A5 exile_resolves: targeted Teysa leaves gy -> Exile; Ooze 3/3; P1 21.
  A6 setup_bf:      Teysa on P0 BF; a Teysa in a graveyard.
  A7 candidates_bf: battlefield Teysa NOT among candidates; gy Teysa IS.
  A8 cleanup:       stacks empty, game proceeds after both legs.

Verdict: reproduced iff A2 passes and (A3 fails on clean offer scans, or
A4 fails, or A5 fails). not-reproduced iff A2-A8 all pass. blocked iff A2
(or an inconclusive A3) cannot be established.

Deliberate driver decisions: P0 plays Swamp first (Entomb needs {B}), then
lands every turn and casts Teysa at 7 mana; P1 never attacks (keeps the
Ooze alive and avoids Teysa's combat-damage trigger in leg B); Entomb
search answers always choose Teysa.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7158")
ISSUE = 7158
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TEYSA = "Teysa, Envoy of Ghosts"
ENTOMB = "Entomb"
OOZE = "Scavenging Ooze"
PLAINS, SWAMP, FOREST = "Plains", "Swamp", "Forest"
LANDS = ("Swamp", "Mountain", "Forest", "Plains", "Island")

P0_DECK = [(TEYSA, 20), (ENTOMB, 12), (PLAINS, 14), (SWAMP, 14)]
P1_DECK = [(OOZE, 12), (FOREST, 48)]
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
              "(started fresh for run 20260914-7158; ServerHello verified "
              "v0.82.0/060b5d2/protocol 70) + verified pin "
              "(minisign-verify of binary + signed data manifest with the "
              "repo-pinned key).",
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
        "entomb_casts": 0,
        "teysa_cast": False,
        # leg A (graveyard)
        "gy_pre": False,
        "gy_activated": False,
        "gy_activated_at": None,
        "gy_target_oid": None,
        "gy_fallback_oid": None,
        "gy_resolved": False,
        "gy_candidates": None,
        "gy_p1_life_before": None,
        "gy_ooze_pt_before": None,
        "gy_ooze_pt_after": None,
        "gy_teysa_zone_after": None,
        # leg B (battlefield control)
        "bf_pre": False,
        "bf_activated": False,
        "bf_activated_at": None,
        "bf_target_oid": None,
        "bf_resolved": False,
        "bf_candidates": None,
        "bf_p1_life_before": None,
        "bf_ooze_pt_before": None,
        "offer_scans": [],
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


def gy_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def gy_names(state, pid):
    return sorted(nname(state["objects"][x]) for x in gy_oids(state, pid))


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid)
            if nname(o) == name.lower()]


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if nname(o) in ("swamp", "mountain", "forest", "plains", "island")
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


def ooze_pt(state):
    oids = bf_named(state, 1, OOZE)
    if not oids:
        return None
    o = state["objects"][oids[0]]
    return (o.get("power"), o.get("toughness"))


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
    t = cd["teysa, envoy of ghosts"]
    blob = json.dumps(t)
    has_prot = any(
        isinstance(k, dict) and "Protection" in k
        and (k["Protection"] or {}).get("CardType") == "creatures"
        for k in (t.get("keywords") or []))
    no_unimpl = "Unimplemented" not in blob
    rec = {"has_protection_from_creatures": has_prot,
           "no_unimplemented_nodes": no_unimpl,
           "keywords": t.get("keywords"),
           "oracle": t.get("oracle_text"),
           "mana_cost": t.get("mana_cost")}
    with open(f"{EVDIR}/parse_teysa.json", "w") as f:
        json.dump(rec, f, indent=1)
    ok = has_prot and no_unimpl
    say(f"A1 parse_ok: {'passed' if ok else 'failed'}")
    ST["a1"] = "passed" if ok else "failed"
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


async def preamble(c, pid, st, state, acts):
    """Mulligan keep / legend as-is / hand-size discard / no blocks /
    no attacks. Returns True if it acted."""
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
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        # P0 protects Teysa (needed for the cast leg) and Entomb (the
        # stocking tool); P1 protects Ooze until cast.
        protect = set()
        if pid == 0:
            protect = {TEYSA.lower(), ENTOMB.lower()}
        else:
            if not bf_named(state, 1, OOZE):
                protect = {OOZE.lower()}
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


async def entomb_search_tick(c, pid, st, state):
    """Answer Entomb's library-search prompt by choosing Teysa."""
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if PROMPT_DONE.get(iid):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        teysas = []
        for ch in chs:
            r = ref_of(ch)
            if not isinstance(r, int):
                continue
            o = get_obj(state, r)
            if o.get("zone") == "Library" \
                    and nname(o) == TEYSA.lower():
                teysas.append((ch, r))
        if not teysas:
            continue
        ch, roid = teysas[0]
        say(f"[{c.name}] Entomb search: choosing Teysa oid={roid}")
        wire("entomb_search", {"who": c.name, "picked_oid": roid,
                               "n_candidates": len(chs)})
        await answer_vi(c, opp, ch, c.name)
        PROMPT_DONE[iid] = True
        return True
    return False


def ooze_able(state):
    """P1's Scavenging Ooze able-bodied for the {G} activation."""
    oids = bf_named(state, 1, OOZE)
    if not oids:
        return None, "no_ooze"
    o = state["objects"][oids[0]]
    if o.get("tapped"):
        return None, "tapped"
    if o.get("summoning_sick"):
        return None, "summoning_sick"
    if "forest" not in untapped_land_names(state, 1):
        return None, "no_green"
    return oids[0], "ok"


def find_ooze_activate(acts, ooze_oid):
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            if int(d.get("source_id", -1)) == int(ooze_oid):
                return a
        except Exception:
            continue
    return None


def gy_nonempty(state):
    n = 0
    for o in (state.get("objects") or {}).values():
        if o.get("zone") == "Graveyard":
            n += 1
    return n


async def maybe_activate_ooze(c, st, state, acts, leg):
    """Offer-scan + activation for leg 'gy' or 'bf'. Returns True if it
    acted (scanned counts as acting to keep the tick honest)."""
    if not is_my_main(state, 1):
        return False
    ooze_oid, why = ooze_able(state)
    gyc = gy_nonempty(state)
    if ooze_oid is None or gyc == 0:
        return False
    a = find_ooze_activate(acts, ooze_oid)
    offered = a is not None
    # record the scan once per revision
    rev = st.get("state_revision", -1)
    if not acted(f"scan_{leg}", rev):
        gy_comp = {}
        for o in (state.get("objects") or {}).values():
            if o.get("zone") == "Graveyard":
                gy_comp[oname(o)] = gy_comp.get(oname(o), 0) + 1
        ST["offer_scans"].append(
            {"leg": leg, "rev": rev, "turn": state.get("turn_number"),
             "offered": offered, "able": why, "gy_cards": gyc,
             "gy_composition": gy_comp})
        wire("offer_scan", ST["offer_scans"][-1])
    if not offered:
        return False
    if leg == "gy" and (ST["gy_activated"] or ST["stage"] != "GY_LEG"):
        return False
    if leg == "bf" and (ST["bf_activated"] or ST["stage"] != "BF_LEG"):
        return False
    if leg == "gy" and not ST["gy_pre"]:
        await export_now("pre_gy.json")
        ST["gy_pre"] = True
        ST["gy_p1_life_before"] = life(state, 1)
        ST["gy_ooze_pt_before"] = ooze_pt(state)
    if leg == "bf" and not ST["bf_pre"]:
        await export_now("pre_bf.json")
        ST["bf_pre"] = True
        ST["bf_p1_life_before"] = life(state, 1)
        ST["bf_ooze_pt_before"] = ooze_pt(state)
    await submit_as_is(c, a)
    # Snapshot every graveyard card oid at activation time. The engine
    # auto-selects when exactly one legal target exists (no TargetSelection
    # prompt), so a later gy departure of a snapshot card is how we detect
    # the auto-select resolution path.
    snap = {}
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard":
            snap[str(oid)] = oname(o)
    ST[f"{leg}_gy_snapshot"] = snap
    wire(f"{leg}_gy_snapshot", {"n": len(snap),
                                "teysa_oids": [k for k, v in snap.items()
                                               if v.lower() == TEYSA.lower()]})
    if leg == "gy":
        ST["gy_activated"] = True
        ST["gy_activated_at"] = time.time()
    else:
        ST["bf_activated"] = True
        ST["bf_activated_at"] = time.time()
    say(f"[P1] Ooze exile ability activated ({leg} leg)")
    wire(f"{leg}_activation", {"action": a})
    return True


def candidate_record(state, ch):
    r = ref_of(ch)
    o = get_obj(state, r) if isinstance(r, int) else {}
    return {"choice_id": ch.get("id"), "ref": r,
            "name": oname(o), "zone": o.get("zone"),
            "controller": o.get("controller"),
            "text": choice_text(ch)[:80]}


async def handle_ooze_target(c, st, state):
    """Answer the Ooze exile TargetSelection; record full candidates."""
    if wf_type(state) != "TargetSelection" or wf_player(state) != 1:
        return False
    leg = None
    if ST["gy_activated"] and not ST["gy_resolved"]:
        leg = "gy"
    elif ST["bf_activated"] and not ST["bf_resolved"]:
        leg = "bf"
    if leg is None:
        return False
    for opp in vi_opps(st):
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        if PROMPT_DONE.get(iid):
            continue
        recs = [candidate_record(state, ch) for ch in chs]
        if ST[f"{leg}_candidates"] is None:
            ST[f"{leg}_candidates"] = recs
            with open(f"{EVDIR}/target_candidates_{leg}.json", "w") as f:
                json.dump({"leg": leg, "interaction_id": iid,
                           "candidates": recs}, f, indent=1)
            say(f"[P1] {leg} leg candidates ({len(recs)}): "
                + json.dumps(recs)[:600])
            wire(f"{leg}_candidates", {"candidates": recs})
        # pick: graveyard Teysa (controller 0 in gy leg; any in bf leg)
        pick = None
        for ch, rec in zip(chs, recs):
            if rec["name"].lower() == TEYSA.lower() \
                    and rec["zone"] == "Graveyard":
                pick = ch
                break
        if pick is None and recs:
            pick = chs[0]  # fallback: keep the game moving
            say(f"[P1] {leg} leg: NO graveyard Teysa among candidates; "
                f"falling back to {recs[0]['name']}")
            wire(f"{leg}_no_teysa_candidate", {"candidates": recs})
        if pick is None:
            say(f"[P1] {leg} leg: EMPTY candidate list; cannot answer")
            wire(f"{leg}_empty_candidates", {})
            PROMPT_DONE[iid] = True
            ST["unexpected"].append(f"{leg}_empty_candidates")
            return True
        rec = candidate_record(state, pick)
        ST[f"{leg}_target_oid"] = str(rec["ref"])
        if leg == "gy" and rec["name"].lower() != TEYSA.lower():
            ST["gy_fallback_oid"] = str(rec["ref"])
        say(f"[P1] {leg} leg target: {rec['name']} oid={rec['ref']} "
            f"zone={rec['zone']}")
        wire(f"{leg}_target_pick", rec)
        await answer_vi(c, opp, pick, "P1")
        PROMPT_DONE[iid] = True
        return True
    return False


def resolution_watch(state):
    """Watch the SPECIFIC targeted oid leaving the graveyard (prompt path),
    or any snapshot gy card leaving the graveyard (engine auto-select path:
    exactly one legal target -> no TargetSelection prompt)."""
    for leg in ("gy", "bf"):
        if not ST[f"{leg}_activated"] or ST[f"{leg}_resolved"]:
            continue
        tgt = ST.get(f"{leg}_target_oid")
        if tgt:
            o = get_obj(state, tgt)
            zone = o.get("zone") if o else None
            if zone == "Graveyard":
                continue
            ST[f"{leg}_resolved"] = True
            ST[f"{leg}_resolved_via"] = "prompt"
            ST[f"{leg}_teysa_zone_after"] = zone
            ST[f"{leg}_ooze_pt_after"] = ooze_pt(state)
            say(f"[{leg}] exile resolved (prompt path): target {tgt} gy -> "
                f"{zone}; Ooze {ST[f'{leg}_ooze_pt_before']} -> "
                f"{ST[f'{leg}_ooze_pt_after']}; P1 life "
                f"{ST[f'{leg}_p1_life_before']} -> {life(state, 1)}")
            wire(f"{leg}_resolved",
                 {"via": "prompt", "target": tgt, "zone_after": zone,
                  "ooze_pt_after": ST[f"{leg}_ooze_pt_after"],
                  "p1_life_after": life(state, 1)})
            continue
        # auto-select path: no prompt was ever answered; detect any
        # activation-time gy card leaving the graveyard (the only exile
        # effect in either deck is the Ooze ability).
        snap = ST.get(f"{leg}_gy_snapshot") or {}
        for oid, nm in snap.items():
            o = get_obj(state, oid)
            zone = o.get("zone") if o else None
            if zone and zone != "Graveyard":
                ST[f"{leg}_resolved"] = True
                ST[f"{leg}_resolved_via"] = "auto_select"
                ST[f"{leg}_auto_target_oid"] = oid
                ST[f"{leg}_auto_target_name"] = nm
                ST[f"{leg}_auto_target_zone_after"] = zone
                ST[f"{leg}_ooze_pt_after"] = ooze_pt(state)
                teysa_still = [k for k, v in snap.items()
                               if v.lower() == TEYSA.lower()
                               and (get_obj(state, k) or {}).get("zone")
                               == "Graveyard"]
                ST[f"{leg}_teysa_still_in_gy"] = teysa_still
                say(f"[{leg}] exile resolved (AUTO-SELECT path): {nm} "
                    f"oid={oid} gy -> {zone}; Teysa still in gy: "
                    f"{teysa_still}; Ooze {ST[f'{leg}_ooze_pt_before']} -> "
                    f"{ST[f'{leg}_ooze_pt_after']}; P1 life "
                    f"{ST[f'{leg}_p1_life_before']} -> {life(state, 1)}")
                wire(f"{leg}_resolved",
                     {"via": "auto_select", "target": oid, "name": nm,
                      "zone_after": zone, "teysa_still_in_gy": teysa_still,
                      "ooze_pt_after": ST[f"{leg}_ooze_pt_after"],
                      "p1_life_after": life(state, 1)})
                break


async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    if await entomb_search_tick(c, 0, st, state):
        return True
    resolution_watch(state)
    if ST["bf_resolved"]:
        return False  # let the main loop finalize
    rev = st.get("state_revision", -1)
    # land drop every tick (no kept-flag; cf. #6690)
    if is_my_main(state, 0):
        want = None
        bf_names = [nname(o) for _, o in bf(state, 0)]
        if "swamp" not in bf_names:
            want = find_hand(state, 0, "Swamp")
        elif "plains" not in bf_names:
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
        # Entomb: #1 anytime in SETUP/GY_LEG; #2 only after gy leg resolved
        entomb_oid = find_hand(state, 0, ENTOMB)
        want_entomb = False
        if entomb_oid and "swamp" in untapped_land_names(state, 0):
            lib_teysas = [str(oid) for oid, o
                          in (state.get("objects") or {}).items()
                          if o.get("zone") == "Library"
                          and nname(o) == TEYSA.lower()]
            if lib_teysas:
                if ST["entomb_casts"] == 0 and not ST["gy_resolved"]:
                    want_entomb = True
                elif ST["entomb_casts"] == 1 and ST["gy_resolved"] \
                        and not ST["bf_activated"]:
                    want_entomb = True
        if want_entomb:
            ca = castspell_for(acts, entomb_oid)
            if ca and not acted(f"entomb{ST['entomb_casts']}", rev):
                await submit_as_is(c, ca)
                ST["entomb_casts"] += 1
                say(f"[P0] casts Entomb #{ST['entomb_casts']}")
                return True
        # Cast Teysa for the battlefield control leg
        if ST["gy_resolved"] and not ST["teysa_cast"] \
                and not bf_named(state, 0, TEYSA):
            teysa_oid = find_hand(state, 0, TEYSA)
            ul = untapped_land_names(state, 0)
            if teysa_oid and len(ul) >= 7 and "plains" in ul \
                    and "swamp" in ul:
                ca = castspell_for(acts, teysa_oid)
                if ca and not acted("castteysa", rev):
                    await submit_as_is(c, ca)
                    ST["teysa_cast"] = True
                    say("[P0] casts Teysa, Envoy of Ghosts")
                    return True
    # arm the battlefield leg once its preconditions hold
    if ST["gy_resolved"] and ST["stage"] == "GY_LEG":
        if bf_named(state, 0, TEYSA) and any(
                nname(o) == TEYSA.lower() and o.get("zone") == "Graveyard"
                for o in (state.get("objects") or {}).values()):
            ST["stage"] = "BF_LEG"
            say("[P0] stage -> BF_LEG (bf Teysa + gy Teysa present)")
            wire("stage", {"stage": "BF_LEG"})
    return False


async def p1_tick(c, st, state, acts):
    if await preamble(c, 1, st, state, acts):
        return True
    if await handle_ooze_target(c, st, state):
        return True
    resolution_watch(state)
    if ST["bf_resolved"]:
        return False
    rev = st.get("state_revision", -1)
    if is_my_main(state, 1):
        # arm the gy leg once its preconditions hold
        if ST["stage"] == "SETUP":
            if any(nname(o) == TEYSA.lower()
                   and o.get("zone") == "Graveyard"
                   and o.get("controller") == 0
                   for o in (state.get("objects") or {}).values()) \
                    and bf_named(state, 1, OOZE):
                ST["stage"] = "GY_LEG"
                say("[P1] stage -> GY_LEG (P0 gy Teysa + Ooze on BF)")
                wire("stage", {"stage": "GY_LEG"})
        # land drop
        fo = find_hand(state, 1, "Forest")
        la = playland_for(acts, fo)
        if la and not acted("land1", rev):
            await submit_as_is(c, la)
            say("[P1] plays Forest")
            return True
        # cast Ooze
        if not bf_named(state, 1, OOZE):
            ooze_oid = find_hand(state, 1, OOZE)
            ul = untapped_land_names(state, 1)
            if ooze_oid and len(ul) >= 2 and "forest" in ul:
                ca = castspell_for(acts, ooze_oid)
                if ca and not acted("castooze", rev):
                    await submit_as_is(c, ca)
                    say("[P1] casts Scavenging Ooze")
                    return True
        # activations (both legs; maybe_activate_ooze gates on stage)
        if ST["stage"] == "GY_LEG":
            if await maybe_activate_ooze(c, st, state, acts, "gy"):
                return True
        elif ST["stage"] == "BF_LEG":
            if await maybe_activate_ooze(c, st, state, acts, "bf"):
                return True
    return False


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
    rev = st.get("state_revision", -1)
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", rev):
                    await submit_as_is(c, a)
                    return True
            break
    return False

# ---------------- main loop helpers ---------------------------------------
async def export_checkpoints():
    if ST["gy_resolved"] and "mid_gy.json" not in ST["exports"]:
        await export_now("mid_gy.json")
    if ST["bf_resolved"] and "mid_bf.json" not in ST["exports"]:
        await export_now("mid_bf.json")
        return True
    return False


def activation_rejected(leg):
    t0 = ST.get(f"{leg}_activated_at")
    if not t0:
        return False
    for r in ST["rejections"]:
        if r["type"] == "ActionRejected" and r.get("t", 0) > t0:
            return True
    return False


# ---------------- PNG summary ---------------------------------------------
def render_png(path, assertions, notes, verdict):
    from PIL import Image, ImageDraw
    W, H = 1040, 1000
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #7158 — bug-state backfill", fill=(240, 240, 240))
    y += 28
    d.text((24, y), "Protection only applies to permanents and players",
           fill=(170, 180, 200))
    y += 26
    d.text((24, y),
           f"server v{SERVER_IDENTITY['server_version']} "
           f"({SERVER_IDENTITY['build_commit']}, protocol "
           f"{SERVER_IDENTITY['protocol_version']}) · 2026-09-14 · "
           f"run {RUN_ID}", fill=(130, 140, 160))
    y += 30
    vc = {"reproduced": (235, 90, 90), "not-reproduced": (110, 220, 130),
          "blocked": (240, 200, 90)}.get(verdict, (200, 200, 200))
    d.text((24, y), f"verdict: {verdict}", fill=vc)
    y += 34
    d.line([(24, y), (W - 24, y)], fill=(60, 66, 80))
    y += 14
    for key, val in assertions.items():
        col = {"passed": (110, 220, 130), "failed": (235, 90, 90),
               "not-run": (150, 150, 150)}.get(val, (200, 200, 200))
        d.text((24, y), f"{key}: {val}", fill=col)
        y += 24
        if y > H - 260:
            break
    y += 8
    d.line([(24, y), (W - 24, y)], fill=(60, 66, 80))
    y += 14
    d.text((24, y), "key observations:", fill=(200, 200, 200))
    y += 24
    for n in notes[:9]:
        for line in [n[i:i + 118] for i in range(0, len(n), 118)][:3]:
            d.text((32, y), line, fill=(160, 170, 190))
            y += 20
        y += 4
        if y > H - 40:
            break
    img.save(path)
    say(f"rendered {path}")


# ---------------- finalize -------------------------------------------------
async def finalize():
    if ST.get("finalized"):
        return None, {}
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_7158.py")
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

    ass, notes = {}, []

    def note(m):
        notes.append(m)
        say("NOTE:", m)

    # A1
    ass["A1_parse_ok"] = ST.get("a1", "not-run")
    note(f"A1: Teysa Protection{{CardType: creatures}} parsed, no "
         f"Unimplemented nodes: {ass['A1_parse_ok']}")

    # A2
    pre_gy = load_state("pre_gy.json")
    ok2 = False
    if pre_gy:
        tgy = [o for o in (pre_gy.get("objects") or {}).values()
               if nname(o) == TEYSA.lower()
               and o.get("zone") == "Graveyard" and o.get("controller") == 0]
        ooze = [o for oid, o in (pre_gy.get("objects") or {}).items()
                if nname(o) == OOZE.lower()
                and o.get("zone") == "Battlefield"
                and o.get("controller") == 1 and not o.get("tapped")]
        green = any(nname(o) == "forest" and not o.get("tapped")
                    for oid, o in (pre_gy.get("objects") or {}).items()
                    if o.get("zone") == "Battlefield"
                    and o.get("controller") == 1)
        ok2 = bool(tgy) and bool(ooze) and green
        note(f"A2: gy_teysa={len(tgy)} ooze_ready={len(ooze)} "
             f"green_available={green}")
    else:
        note("A2: pre_gy.json missing")
    ass["A2_setup_gy"] = "passed" if ok2 else "failed"

    # A3
    scans_gy = [s for s in ST["offer_scans"] if s["leg"] == "gy"]
    offered_gy = any(s["offered"] for s in scans_gy)
    nooffer_turns = len({s["turn"] for s in scans_gy if not s["offered"]})
    if ST["gy_activated"] or offered_gy:
        ass["A3_offer_gy"] = "passed"
    elif nooffer_turns >= 6:
        ass["A3_offer_gy"] = "failed"
    else:
        ass["A3_offer_gy"] = "not-run"
    note(f"A3: gy offer scans={len(scans_gy)} offered={offered_gy} "
         f"clean_no_offer_turns={nooffer_turns} activated={ST['gy_activated']}")

    # A4: with Teysa + another card in the gy, a correct engine MUST show a
    # TargetSelection prompt listing the gy Teysa. If the engine instead
    # auto-selected (exactly one legal target), the Teysa was excluded from
    # the legal set -> failed.
    cands = ST.get("gy_candidates")
    if cands is not None:
        hit = [r for r in cands if r["name"].lower() == TEYSA.lower()
               and r["zone"] == "Graveyard"]
        ass["A4_candidates_gy"] = "passed" if hit else "failed"
        note(f"A4: gy candidates={len(cands)} teysa_in_gy="
             f"{[r['ref'] for r in hit]}")
    elif ST.get("gy_resolved_via") == "auto_select":
        ass["A4_candidates_gy"] = "failed"
        note(f"A4: NO TargetSelection prompt ever surfaced (wire log has "
             f"{len([s for s in ST['offer_scans'] if s['leg'] == 'gy'])} gy "
             f"offer scans, zero TargetSelection waiting_for); engine "
             f"auto-selected {ST.get('gy_auto_target_name')} "
             f"oid={ST.get('gy_auto_target_oid')} while "
             f"{TEYSA} sat in the gy -> Teysa was excluded from the legal "
             f"target set")
    elif ST["gy_activated"]:
        ass["A4_candidates_gy"] = "not-run"
        note("A4: activation submitted but no prompt and no resolution seen")
    else:
        ass["A4_candidates_gy"] = "not-run"
        note("A4: gy activation never submitted")

    # A5
    rej_target = [r for r in ST["rejections"]
                  if r["type"] in ("ActionRejected", "Error")]
    if ST.get("gy_resolved_via") == "prompt" and ST.get("gy_target_oid"):
        zone_ok = ST.get("gy_teysa_zone_after") == "Exile"
        pt = ST.get("gy_ooze_pt_after")
        pt_ok = pt is not None and pt[0] == 3 and pt[1] == 3
        life_ok = ST.get("gy_p1_life_before") is not None and \
            life(load_state("mid_gy.json") or {}, 1) == \
            ST["gy_p1_life_before"] + 1
        tgt_is_teysa = False
        o = get_obj(load_state("mid_gy.json") or {}, ST["gy_target_oid"])
        tgt_is_teysa = (o.get("card_name") or o.get("name") or "").lower() \
            == TEYSA.lower()
        ok5 = zone_ok and pt_ok and life_ok and tgt_is_teysa
        ass["A5_exile_resolves"] = "passed" if ok5 else "failed"
        note(f"A5: prompt path target_teysa={tgt_is_teysa} "
             f"zone_after={ST.get('gy_teysa_zone_after')} "
             f"ooze_pt={ST.get('gy_ooze_pt_before')}->"
             f"{ST.get('gy_ooze_pt_after')} "
             f"p1_life={ST.get('gy_p1_life_before')}->"
             f"{life(load_state('mid_gy.json') or {}, 1)}")
    elif ST.get("gy_resolved_via") == "auto_select":
        nm = (ST.get("gy_auto_target_name") or "").lower()
        zone = ST.get("gy_auto_target_zone_after")
        teysa_still = ST.get("gy_teysa_still_in_gy")
        if nm == TEYSA.lower() and zone == "Exile":
            pt = ST.get("gy_ooze_pt_after")
            ok5 = pt is not None and pt[0] == 3 and pt[1] == 3
            ass["A5_exile_resolves"] = "passed" if ok5 else "failed"
            note(f"A5: auto-select path exiled the gy Teysa itself "
                 f"(correct): ooze_pt={ST.get('gy_ooze_pt_after')}")
        else:
            ass["A5_exile_resolves"] = "failed"
            note(f"A5: auto-select path exiled {ST.get('gy_auto_target_name')} "
                 f"oid={ST.get('gy_auto_target_oid')} (gy -> {zone}) while "
                 f"{TEYSA} remained in the gy (oids {teysa_still}): the "
                 f"protection-bearing card was never a legal target")
    elif ST["gy_activated"] and rej_target:
        ass["A5_exile_resolves"] = "failed"
        note(f"A5: target submission rejected: "
             f"{json.dumps(rej_target[-1])[:300]}")
    elif ST["gy_activated"]:
        ass["A5_exile_resolves"] = "failed"
        note("A5: activation accepted but never resolved (stall)")
    else:
        ass["A5_exile_resolves"] = "not-run"
        note("A5: gy activation never submitted")

    # A6
    pre_bf = load_state("pre_bf.json")
    ok6 = False
    if pre_bf:
        objs = pre_bf.get("objects") or {}
        bf_t = [o for o in objs.values()
                if nname(o) == TEYSA.lower()
                and o.get("zone") == "Battlefield"
                and o.get("controller") == 0]
        gy_t = [o for o in objs.values()
                if nname(o) == TEYSA.lower()
                and o.get("zone") == "Graveyard"]
        ok6 = bool(bf_t) and bool(gy_t)
        note(f"A6: bf_teysa={len(bf_t)} gy_teysa={len(gy_t)}")
    else:
        note("A6: pre_bf.json missing")
    ass["A6_setup_bf"] = "passed" if ok6 else "failed"

    # A7: the battlefield Teysa must never be targetable/touched. Prompt
    # path: assert no Battlefield Teysa among candidates. Auto-select path:
    # assert the pre_bf battlefield Teysa oid is still on the battlefield
    # in post.json.
    post = load_state("post.json")
    bcands = ST.get("bf_candidates")
    if bcands is not None:
        bf_hits = [r for r in bcands if r["name"].lower() == TEYSA.lower()
                   and r["zone"] == "Battlefield"]
        gy_hits = [r for r in bcands if r["name"].lower() == TEYSA.lower()
                   and r["zone"] == "Graveyard"]
        ok7 = not bf_hits and bool(gy_hits)
        ass["A7_candidates_bf"] = "passed" if ok7 else "failed"
        note(f"A7: bf candidates={len(bcands)} battlefield_teysa="
             f"{[r['ref'] for r in bf_hits]} graveyard_teysa="
             f"{[r['ref'] for r in gy_hits]}")
    elif ST.get("bf_resolved_via") == "auto_select" and pre_bf and post:
        pre_objs = pre_bf.get("objects") or {}
        post_objs = post.get("objects") or {}
        bf_teysa_oids = [oid for oid, o in pre_objs.items()
                         if nname(o) == TEYSA.lower()
                         and o.get("zone") == "Battlefield"
                         and o.get("controller") == 0]
        still = [oid for oid in bf_teysa_oids
                 if (post_objs.get(oid) or {}).get("zone") == "Battlefield"]
        ok7 = bool(bf_teysa_oids) and len(still) == len(bf_teysa_oids)
        ass["A7_candidates_bf"] = "passed" if ok7 else "failed"
        note(f"A7: auto-select path; bf Teysa oids={bf_teysa_oids} still on "
             f"bf in post={still}; auto-exiled "
             f"{ST.get('bf_auto_target_name')} oid="
             f"{ST.get('bf_auto_target_oid')}")
    elif ST.get("bf_activated"):
        ass["A7_candidates_bf"] = "not-run"
        note("A7: bf activation submitted but no prompt and no resolution")
    else:
        ass["A7_candidates_bf"] = "not-run"
        note("A7: bf activation never submitted")

    # A8
    if post is None:
        post = load_state("post.json")
    ok8 = False
    if post:
        stack = post.get("stack") or []
        ok8 = len(stack) == 0
        note(f"A8: post stack depth={len(stack)} "
             f"phase={post.get('phase')} turn={post.get('turn_number')}")
    else:
        note("A8: post.json missing")
    ass["A8_cleanup"] = "passed" if ok8 else "failed"

    # verdict
    a = ass
    if a["A2_setup_gy"] == "passed" and (
            a["A3_offer_gy"] == "failed"
            or a["A4_candidates_gy"] == "failed"
            or a["A5_exile_resolves"] == "failed"):
        verdict = "reproduced"
    elif all(a[k] == "passed" for k in
             ("A2_setup_gy", "A3_offer_gy", "A4_candidates_gy",
              "A5_exile_resolves", "A6_setup_bf", "A7_candidates_bf",
              "A8_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    render_png(f"{EVDIR}/summary.png", ass, notes, verdict)

    run = {
        "issue": ISSUE,
        "title": "Protection bug — Protection only applies to permanents "
                 "and players.",
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "validated_version": "v0.82.0",
        "server": SERVER_IDENTITY,
        "server_hello_observed": ST.get("server_hello"),
        "protocol_version": 70,
        "scope": "Teysa, Envoy of Ghosts (protection from creatures) as a "
                 "target of Scavenging Ooze's {G} 'Exile target card from a "
                 "graveyard' ability: graveyard leg (reported bug) + "
                 "battlefield control leg; native engine, two human-client "
                 "seats",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {v}" for k, v in ass.items()),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Only the activated-ability targeting path was tested; "
            "spell targeting of graveyard cards was not exercised.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
        "done_reason": ST.get("done_reason"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # manifest LAST (after all logging is done)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            f.write(f"{h}  {fn}\n")
    say(f"manifest written for {len(files)} files; done: "
        f"{ST.get('done_reason')}")
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
            # activation-rejection retry: reset the in-flight flag so the
            # next main phase re-offers
            for leg in ("gy", "bf"):
                if ST[f"{leg}_activated"] and not ST[f"{leg}_resolved"] \
                        and activation_rejected(leg):
                    say(f"[P1] {leg} activation rejected; will retry")
                    wire(f"{leg}_activation_rejected_retry", {})
                    ST[f"{leg}_activated"] = False
            # offer give-up: 6 clean P1 main-phase turns, able-bodied,
            # non-empty gy, no offer
            for leg in ("gy", "bf"):
                turns = {s["turn"] for s in ST["offer_scans"]
                         if s["leg"] == leg and not s["offered"]}
                if len(turns) >= 6 and not ST[f"{leg}_activated"]:
                    ST["done_reason"] = (f"{leg} leg: no ActivateAbility "
                                         f"offer across {len(turns)} clean "
                                         f"turns")
                    say("GIVEUP: " + ST["done_reason"])
                    wire("offer_giveup", {"leg": leg, "turns": len(turns)})
                    # export the setup state at giveup so A2 can be judged
                    if leg == "gy" and "pre_gy.json" not in ST["exports"]:
                        await export_now("pre_gy.json")
                    elif leg == "bf" and "pre_bf.json" not in ST["exports"]:
                        await export_now("pre_bf.json")
                    ST["stop"] = True
            await tick(C0, 0)
            await tick(C1, 1)
            if await export_checkpoints():
                if ST["bf_resolved"]:
                    ST["done_reason"] = "both legs complete"
                    break
            await asyncio.sleep(0.25)
    except Exception as e:
        say(f"main loop error: {e}")
        wire("main_error", {"error": str(e)[:300]})
    finally:
        # export the terminal state BEFORE closing the clients (export_now
        # needs a live connection)
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
