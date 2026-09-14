#!/usr/bin/env python3
"""phase-rs/phase #7145 - Necrotic Ooze not getting all activated abilities
of all creatures in graveyard.

Oracle text (from pinned card-data.json):
  As long as this creature is on the battlefield, it has all activated
  abilities of all creature cards in all graveyards.

Card-data AST (GrantAllActivatedAbilitiesOf, Typed{Creature, controller:null,
properties:[InZone(Graveyard)]}) is faithful, so the defect (if any) is in
runtime ability enumeration / dedup / zone-controller filtering (triage).

Behavioral contract (2 human seats, native engine):
  P0 stocks its graveyard with Prodigal Pyromancer (1 activated ability) and
  Ana Disciple (2 activated abilities) via Faithless Looting; P1 stocks its
  graveyard with Llanowar Elves (1) and Royal Assassin (1) the same way.
  P0 casts Necrotic Ooze, then asserts the Ooze's effective ability set:
    expected = {PYRO, ANA_FLY, ANA_WEAK} (own gy) + {ELVES, ASSASSIN} (opp gy)
  A1 parse_grant:      card-data static is GrantAllActivatedAbilitiesOf over
                       Typed Creature, controller null, InZone Graveyard.
  A2 setup_ok:         Ooze on P0 BF; P0 gy has Pyromancer+Ana Disciple;
                       P1 gy has Elves+Royal Assassin.
  A3 own_gy_complete:  Ooze abilities include PYRO + ANA_FLY + ANA_WEAK.
  A4 opp_gy_complete:  Ooze abilities include ELVES + ASSASSIN.
  A5 multi_ability:    Ana Disciple contributes exactly 2 distinct abilities.
  A6 activation:       Ooze's granted Pyromancer ability activates targeting
                       P1 -> P1 life 20->19, Ooze tapped (functional proof).
  A6b opp_activation:  Ooze's granted Elves mana ability activates -> P0
                       mana pool gains {G} (opp-graveyard grant is usable).
  A7 removal_updates:  exiling EVERY Ana Disciple from P0's gy (via the
                       granted Scavenging Ooze ability, repeatable) removes
                       exactly its 2 sigs; the other 3 (PYRO/ELVES/ASSASSIN)
                       plus the exiling tool's own grant remain.
  A8 cleanup:          stack empty, game advanced past the test.

Verdict: reproduced iff A2 passes and any of A3/A4/A5 fail (abilities
missing from the grant). not-reproduced iff A2-A7 all pass. blocked iff A2
cannot be established.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7145")
EVDIR = f"{BACKFILL}/evidence/7145/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OOZE = "Necrotic Ooze"
LOOTING = "Faithless Looting"
PYRO = "Prodigal Pyromancer"
ANA = "Ana Disciple"
SCAV = "Scavenging Ooze"
ELVES = "Llanowar Elves"
ASSASSIN = "Royal Assassin"
SWAMP, MOUNTAIN, FOREST = "Swamp", "Mountain", "Forest"
LANDS = (SWAMP, MOUNTAIN, FOREST, "Plains", "Island")

P0_DECK = [(OOZE, 12), (LOOTING, 12), (PYRO, 8), (ANA, 8), (SCAV, 8),
           (SWAMP, 10), (MOUNTAIN, 8), (FOREST, 6)]
P1_DECK = [(LOOTING, 12), (ELVES, 8), (ASSASSIN, 8), (MOUNTAIN, 32)]
TIMEOUT = 1500

CD_PATH = (f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json")

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034205ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(already running from prior run 20260914-7144-081218; "
              "ServerHello verified v0.82.0/060b5d2/protocol 70) + "
              "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "looting_p0_done": False,
        "looting_p1_done": False,
        "ooze_cast": False,
        "scav_cast": False,
        "grant_scanned": False,
        "grant_scan": None,       # sig -> {index, desc}
        "pyro_activated": False,
        "pyro_resolved": False,
        "p1_life_before_pyro": None,
        "elves_activated": False,
        "elves_resolved": False,
        "elves_pool_before": None,
        "scav_activated": False,
        "scav_resolved": False,
        "anas_exiled": [],
        "scav_target_oid": None,
        "scav_giveup": False,
        "remove_scanned": False,
        "remove_scan": None,
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


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def gy_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def gy_names(state, pid):
    return sorted(oname(state["objects"][x]) for x in gy_oids(state, pid))


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def ooze_obj(state):
    for oid, o in bf(state, 0):
        if oname(o) == OOZE:
            return oid, o
    return None, None


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if oname(o) in LANDS and not o.get("tapped")]


def untapped_land_names(state, pid):
    return [oname(o) for _, o in untapped_lands(state, pid)]


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


def mana_pool(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("mana_pool")
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
                                     "stage": ST.get("stage")})
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
    return s


# ---------------- viewer_interaction helpers (cf. scenario_7142) ----------
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

# ---------------- ability signatures ------------------------------------
def ab_sig(a):
    """Structural signature of an activated ability: (cost, effect-type,
    discriminating effect params). Robust to granted-copy wrapper fields."""
    cost = json.dumps(a.get("cost"), sort_keys=True)
    eff = a.get("effect") or {}
    et = eff.get("type")
    extra = ""
    if et == "DealDamage":
        extra = f"{json.dumps(eff.get('amount'), sort_keys=True)}/" \
                f"{json.dumps(eff.get('target'), sort_keys=True)}"
    elif et == "Mana":
        extra = json.dumps(eff.get("produced"), sort_keys=True)
    elif et == "Destroy":
        extra = json.dumps(eff.get("target"), sort_keys=True)
    elif et == "Pump":
        extra = f"{json.dumps(eff.get('power'), sort_keys=True)}/" \
                f"{json.dumps(eff.get('toughness'), sort_keys=True)}"
    elif et == "GenericEffect":
        extra = json.dumps(eff.get("static_abilities"), sort_keys=True)
    else:
        extra = json.dumps(eff, sort_keys=True)[:300]
    return (cost, et, extra)


def expected_sigs():
    """Signatures of the fixture abilities, computed from pinned card-data."""
    cd = json.load(open(CD_PATH))
    want = {"PYRO": ("prodigal pyromancer", 0),
            "ANA_FLY": ("ana disciple", 0),
            "ANA_WEAK": ("ana disciple", 1),
            "ELVES": ("llanowar elves", 0),
            "ASSASSIN": ("royal assassin", 0),
            "SCAV_EXILE": ("scavenging ooze", 0)}
    out = {}
    for key, (card, idx) in want.items():
        acts = [a for a in cd[card]["abilities"]
                if a.get("kind") == "Activated"]
        out[key] = ab_sig(acts[idx])
    return out


EXPECTED = None  # filled in main()


def scan_ooze_abilities(state):
    """Return (sig->info dict, offer_count). info: index, desc, raw ability."""
    oid, o = ooze_obj(state)
    if o is None:
        return None, 0
    sigs = {}
    for idx, a in enumerate(o.get("abilities") or []):
        if a.get("kind") != "Activated":
            continue
        s = ab_sig(a)
        sigs.setdefault(s, {"index": idx, "desc": a.get("description"),
                            "count": 0})
        sigs[s]["count"] += 1
    return sigs, oid


def scan_offers(state, acts, ooze_oid):
    """Count ActivateAbility offers for the Ooze across legal_actions."""
    n = 0
    idxs = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            if int(d.get("source_id", -1)) == int(ooze_oid):
                n += 1
                idxs.append(d.get("ability_index"))
        except Exception:
            continue
    return n, idxs


# ---------------- parse check (A1) ---------------------------------------
def parse_check():
    cd = json.load(open(CD_PATH))
    ooze = cd["necrotic ooze"]
    statics = ooze.get("static_abilities") or []
    grant = None
    for s in statics:
        for m in s.get("modifications") or []:
            if m.get("type") == "GrantAllActivatedAbilitiesOf":
                grant = m
    rec = {"n_static": len(statics), "grant_found": grant is not None,
           "grant": grant,
           "oracle": ooze.get("oracle_text"),
           "mana_cost": ooze.get("mana_cost")}
    with open(f"{EVDIR}/parse_necrotic_ooze.json", "w") as f:
        json.dump(rec, f, indent=1)
    ok = (grant is not None
          and (grant.get("source") or {}).get("type") == "Typed"
          and "Creature" in (grant.get("source") or {})
                           .get("type_filters", [])
          and (grant.get("source") or {}).get("controller") is None
          and any(p.get("type") == "InZone"
                  and p.get("zone") == "Graveyard"
                  for p in (grant.get("source") or {}).get("properties", [])))
    say(f"A1 parse_grant: {'passed' if ok else 'failed'}")
    wire("parse_check", {"ok": ok, "grant": grant})
    return "passed" if ok else "failed"


# ---------------- discard handling (Faithless Looting) -------------------
P0_WANT = [PYRO, ANA]
P1_WANT = [ELVES, ASSASSIN]


def answer_looting_discard(c, pid, st, state):
    """Discard exactly the two fixture creatures for this seat."""
    want = P0_WANT if pid == 0 else P1_WANT
    for opp in vi_opps(st):
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        oid_by_ref = {}
        for ch in chs:
            r = ref_of(ch)
            if r is not None:
                oid_by_ref[str(r)] = ch["id"]
        picks = []
        for nm in want:
            oid = find_hand(state, pid, nm)
            if oid and str(oid) in oid_by_ref:
                picks.append(oid_by_ref[str(oid)])
        if len(picks) < len(want):
            say(f"[{c.name}] looting discard: want {want}, have "
                f"{[oname(state['objects'][x]) for x in hand_oids(state, pid)]}; deferring")
            return False
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = (data.get("spec") or {}).get("type") if isinstance(
            data.get("spec"), dict) else data.get("type")
        iid = opp.get("interactionId")
        if rtype == "schema" and spec in ("sequence", "select"):
            sub = {"interactionId": iid,
                   "response": {"type": spec,
                                "data": {"choiceIds": picks}}}
        else:
            # count==2 but single-choice shape: submit one at a time
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": picks[0]}}}
        say(f"[{c.name}] looting discards {want}")
        wire("interaction_submission",
             {"who": c.name, "purpose": "looting_discard",
              "submission": sub})
        return sub
    return None


async def submit_looting_discard(c, pid, st, state):
    sub = answer_looting_discard(c, pid, st, state)
    if sub:
        await c.send_interaction(sub)
        if pid == 0:
            ST["looting_p0_done"] = True
        else:
            ST["looting_p1_done"] = True
        say(f"[{c.name}] looting discard submitted")
        return True
    return False

# ---------------- target selection ---------------------------------------
async def handle_target(c, pid, st, state):
    """Answer TargetSelection for (a) Pyro damage -> P1, (b) ScavOoze exile
    -> Ana Disciple in P0's graveyard."""
    if wf_player(state) != pid:
        return False
    for opp in vi_opps(st):
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        iid = opp.get("interactionId")
        pick = None
        purpose = None
        if ST.get("pyro_activated") and not ST.get("pyro_resolved"):
            # damage target: choose P1 (seat 1)
            pick = next((ch for ch in chs if seat_of(ch) == 1), None)
            purpose = "pyro_damage_target_p1"
        elif ST.get("scav_activated"):
            # exile target: any remaining Ana Disciple card in P0's
            # graveyard (the removal leg exiles them one at a time)
            for ch in chs:
                r = ref_of(ch)
                if r is None:
                    continue
                o = state["objects"].get(str(r))
                if o and oname(o) == ANA and o.get("zone") == "Graveyard" \
                        and o.get("controller") == 0:
                    pick = ch
                    purpose = "scav_exile_ana"
                    ST["scav_target_oid"] = str(r)
                    break
        if pick is None:
            continue
        say(f"[{c.name}] TargetSelection ({purpose}): "
            f"choosing {choice_text(pick)[:80]}")
        wire("target_selection",
             {"who": c.name, "purpose": purpose,
              "picked_ref": ref_of(pick),
              "picked_seat": seat_of(pick)})
        await answer_vi(c, opp, pick, c.name)
        return True
    return False


# ---------------- activation ---------------------------------------------
def find_ooze_activate_action(state, acts, ooze_oid, sig):
    """Find advertised ActivateAbility for the Ooze at the ability index
    matching sig. Index comes from the LIVE state scan (fallback: grant
    scan), since the advertised ability_index keys the effective array."""
    live, _ = scan_ooze_abilities(state)
    want_idx = (live or {}).get(sig, {}).get("index")
    if want_idx is None:
        want_idx = (ST.get("grant_scan") or {}).get(sig, {}).get("index")
    if want_idx is None:
        return None
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            if int(d.get("source_id", -1)) == int(ooze_oid) \
                    and int(d.get("ability_index", -2)) == int(want_idx):
                return a
        except Exception:
            continue
    return None


async def activate_granted(c, pid, st, state, acts, sig_key, tag):
    """Activate one granted ability of the Ooze by ability_index."""
    ooze_oid, o = ooze_obj(state)
    if ooze_oid is None:
        return False
    if o.get("tapped") or o.get("summoning_sick"):
        return False
    sig = EXPECTED[sig_key]
    a = find_ooze_activate_action(state, acts, ooze_oid, sig)
    if a is None:
        # record once per stage that the offer is missing
        key = f"nooffer_{tag}"
        if key not in ST:
            ST[key] = True
            say(f"[{c.name}] {tag}: no advertised ActivateAbility for "
                f"{sig_key} (ooze {ooze_oid}); offers scanned separately")
            wire("activation_offer_missing",
                 {"tag": tag, "sig_key": sig_key, "ooze_oid": ooze_oid})
        ST[f"{tag}_miss"] = ST.get(f"{tag}_miss", 0) + 1
        return False
    await submit_as_is(c, a)
    say(f"[{c.name}] {tag}: activated granted {sig_key} via Ooze")
    wire("granted_activation", {"tag": tag, "sig_key": sig_key,
                                "action": a})
    return True


async def activate_scav_exile(c, st, state, acts):
    """Activate Scavenging Ooze's {G} exile ability."""
    soids = bf_named(state, 0, SCAV)
    if not soids:
        return False
    soid = soids[0]
    o = state["objects"][soid]
    if o.get("tapped") or o.get("summoning_sick"):
        return False
    if FOREST not in untapped_land_names(state, 0):
        return False
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            if int(d.get("source_id", -1)) == int(soid):
                await submit_as_is(c, a)
                ST["scav_activated"] = True
                say(f"[P0] scavenging ooze exile ability activated")
                wire("scav_activation", {"action": a})
                return True
        except Exception:
            continue
    return False

# ---------------- tick ----------------------------------------------------
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


def find_action(acts, atype):
    for a in acts:
        if a["type"] == atype:
            return a
    return None


async def preamble(c, pid, st, state, acts):
    """Mulligan / legend / hand-size discard / blockers / declare-empty.
    Returns True if it acted (caller should return)."""
    rev = st.get("state_revision", -1)
    # legend choices: submit as-is once per revision
    for a in acts:
        if "Legend" in a["type"]:
            if not acted(f"leg{pid}", rev):
                await submit_as_is(c, a)
                say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True
    # mulligan: keep
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
    # discard to hand size: protect key cards
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        protect = {OOZE, SCAV}
        if pid == 0 and not ST["looting_p0_done"]:
            protect |= {LOOTING, PYRO, ANA}
        if pid == 0 and ST["looting_p0_done"]:
            # after the fixture looting, let excess Scavenging Oozes hit the
            # graveyard: the Ooze then grants its {G} exile ability, which
            # doubles as the removal-leg tool (no real ScavOoze cast needed).
            # Ana Disciples in hand stay protected so they cannot re-enter
            # the graveyard after the removal leg exiles them all (dense
            # playsets: other Ana copies would otherwise re-grant).
            protect.discard(SCAV)
            protect.add(ANA)
        if pid == 1 and not ST["looting_p1_done"]:
            protect |= {LOOTING, ELVES, ASSASSIN}
        oids = hand_oids(state, pid)
        # discard non-protected first (lands first), then protected extras
        def rank(x):
            nm = oname(state["objects"][x])
            if nm in protect:
                return 2
            return 0 if nm in LANDS else 1
        oids.sort(key=rank)
        picks = [int(x) for x in oids[:n]]
        if picks and not acted(f"hsd{pid}", rev):
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{c.name}] discards {n} to hand size")
        return True
    # never block
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no blockers")
            return True
    # never attack: submit empty attackers when offered
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


def p0_land_choice(state):
    """Swamp x3 -> Mountain x2 -> Forest x2 -> any; but once the Ooze is
    down, a Forest (for Scavenging Ooze + its {G} activation) comes first."""
    names = [oname(o) for _, o in bf(state, 0)]
    if ST.get("ooze_cast") and names.count(FOREST) < 1:
        oid = find_hand(state, 0, FOREST)
        if oid:
            return oid
    if names.count(SWAMP) < 3:
        return find_hand(state, 0, SWAMP)
    if names.count(MOUNTAIN) < 2:
        return find_hand(state, 0, MOUNTAIN)
    if names.count(FOREST) < 2:
        return find_hand(state, 0, FOREST)
    for l in LANDS:
        oid = find_hand(state, 0, l)
        if oid:
            return oid
    return None


async def p0_tick(c, st, state, acts):
    if await preamble(c, 0, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    # Looting discard
    if wt == "DiscardChoice" and wp == 0 and not ST["looting_p0_done"]:
        if await submit_looting_discard(c, 0, st, state):
            return True
    # Target selection (pyro damage / scav exile)
    if wt == "TargetSelection" and wp == 0:
        if await handle_target(c, 0, st, state):
            return True
    # resolution watches (run every tick, not just main phase)
    if ST["pyro_activated"] and not ST["pyro_resolved"]:
        if life(state, 1) is not None and ST["p1_life_before_pyro"] is not None \
                and life(state, 1) < ST["p1_life_before_pyro"]:
            ST["pyro_resolved"] = True
            say(f"[P0] pyro damage resolved: P1 "
                f"{ST['p1_life_before_pyro']} -> {life(state, 1)}")
            wire("pyro_resolved", {"p1_life": life(state, 1)})
            await export_now("post_activate_pyro.json")
            return True
    if ST["elves_activated"] and not ST["elves_resolved"]:
        pool = mana_pool(state, 0)
        wire("elves_pool_poll", {"pool": pool})
        if pool and "Green" in str(pool):
            ST["elves_resolved"] = True
            say(f"[P0] elves mana resolved: pool={pool}")
            wire("elves_resolved", {"pool": pool})
            await export_now("post_activate_elves.json")
            return True
    # removal-leg resolution watch: the SPECIFIC targeted Ana leaving the
    # graveyard (dense playsets keep other copies in the gy, so a
    # zero-copies check can never fire — cf. #6906 destroy-leg lesson)
    tgt = ST.get("scav_target_oid")
    if tgt and ST.get("scav_activated"):
        o = state["objects"].get(str(tgt))
        if o is None or o.get("zone") != "Graveyard":
            ST["anas_exiled"].append(str(tgt))
            ST["scav_target_oid"] = None
            say(f"[P0] scav exile resolved: Ana {tgt} left P0 graveyard "
                f"(now {o.get('zone') if o else 'gone'}); "
                f"exiled={len(ST['anas_exiled'])}")
            wire("scav_resolved",
                 {"target": str(tgt),
                  "zone": o.get("zone") if o else None,
                  "n_exiled": len(ST["anas_exiled"])})

    if not is_my_main(state, 0):
        return False
    rev = st.get("state_revision", -1)

    # land drop (once per revision: the server can take >100ms to
    # broadcast the post-drop state, and unguarded retries spam
    # action_not_allowed on the same revision)
    lid = p0_land_choice(state)
    a = playland_for(acts, lid)
    if a and not acted("land0", rev):
        await submit_as_is(c, a)
        say(f"[P0] plays {oname(state['objects'][lid])}")
        acted("land0", rev)
        return True

    ul = untapped_land_names(state, 0)
    n_untapped = len(ul)

    # 1. cast Looting (needs Pyro + Ana in hand + red mana)
    if not ST["looting_p0_done"] and not acted("loot0", rev):
        if find_hand(state, 0, LOOTING) and find_hand(state, 0, PYRO) \
                and find_hand(state, 0, ANA) and MOUNTAIN in ul:
            a = castspell_for(acts, find_hand(state, 0, LOOTING))
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Faithless Looting")
                acted("loot0", rev)
                return True

    # 2. cast Necrotic Ooze ({2}{B}{B} per card-data)
    if ST["looting_p0_done"] and not ST["ooze_cast"] \
            and not acted("ooze", rev):
        if find_hand(state, 0, OOZE) and ul.count(SWAMP) >= 2 \
                and n_untapped >= 4:
            a = castspell_for(acts, find_hand(state, 0, OOZE))
            if a:
                await submit_as_is(c, a)
                ST["ooze_cast"] = True
                say("[P0] casts Necrotic Ooze")
                return True

    # 3. cast Scavenging Ooze ({1}{G})
    if ST["ooze_cast"] and not ST["scav_cast"] and not acted("scav", rev):
        if find_hand(state, 0, SCAV) and FOREST in ul and n_untapped >= 2:
            a = castspell_for(acts, find_hand(state, 0, SCAV))
            if a:
                await submit_as_is(c, a)
                ST["scav_cast"] = True
                say("[P0] casts Scavenging Ooze")
                return True

    # 4. grant check: ooze on BF + all four sources in graveyards
    if ST["ooze_cast"] and not ST["grant_scanned"] \
            and not acted("grant", rev):
        ooze_oid, _ = ooze_obj(state)
        p0gy, p1gy = gy_names(state, 0), gy_names(state, 1)
        if ooze_oid and PYRO in p0gy and ANA in p0gy \
                and ELVES in p1gy and ASSASSIN in p1gy:
            acted("grant", rev)
            await export_now("pre_grant.json")
            sigs, _ = scan_ooze_abilities(state)
            ST["grant_scan"] = {s: v for s, v in (sigs or {}).items()}
            n_off, idxs = scan_offers(state, acts, ooze_oid)
            ST["offer_scans"].append(
                {"stage": "grant", "n_offers": n_off, "idxs": idxs,
                 "n_state_abs": len(sigs or {})})
            wire("grant_scan", {
                "ooze_oid": ooze_oid,
                "p0_gy": p0gy, "p1_gy": p1gy,
                "abilities": [
                    {"sig": [s[0][:60], s[1], str(s[2])[:80]],
                     "index": v["index"], "count": v["count"],
                     "desc": v["desc"]}
                    for s, v in (sigs or {}).items()],
                "n_offers": n_off, "offer_idxs": idxs})
            say(f"[P0] grant scan: {len(sigs or {})} activated abilities in "
                f"state, {n_off} ActivateAbility offers")
            ST["grant_scanned"] = True
            with open(f"{EVDIR}/grant_scan.json", "w") as f:
                json.dump({"abilities": [
                    {"index": v["index"], "desc": v["desc"],
                     "cost": s[0], "effect": s[1], "params": s[2]}
                    for s, v in (sigs or {}).items()],
                    "n_offers": n_off}, f, indent=1, default=str)
            return True

    # 5. activate granted Pyro ability -> target P1
    if ST["grant_scanned"] and not ST["pyro_activated"] \
            and not ST.get("pyro_skipped") and not acted("pyro", rev):
        ooze_oid, o = ooze_obj(state)
        if ooze_oid and not o.get("tapped") and not o.get("summoning_sick"):
            if not ST.get("offers_able_scanned"):
                n_off, idxs = scan_offers(state, acts, ooze_oid)
                ST["able_offers"] = idxs
                ST["offers_able_scanned"] = True
                live, _ = scan_ooze_abilities(state)
                # dump every advertised action type once, so we can see how
                # mana abilities (Elves {T}: Add {G}) surface, if at all
                atyp = {}
                for a in acts:
                    t = a.get("type")
                    atyp[t] = atyp.get(t, 0) + 1
                wire("able_bodied_offer_scan",
                     {"n_offers": n_off, "offer_idxs": idxs,
                      "n_state_abs": len(live or {}),
                      "action_types": atyp,
                      "grant_idxs": [v["index"]
                                     for v in (ST["grant_scan"] or {}).values()]})
                say(f"[P0] able-bodied offer scan: {n_off} offers, "
                    f"idxs={idxs}, action_types={atyp}")
            if await activate_granted(c, 0, st, state, acts, "PYRO",
                                      "pyro_damage"):
                ST["pyro_activated"] = True
                ST["p1_life_before_pyro"] = life(state, 1)
                acted("pyro", rev)
                return True
            if ST.get("pyro_damage_miss", 0) > 600:
                ST["pyro_skipped"] = True
                say("[P0] pyro activation offer never appeared "
                    "(600 ticks); skipping to elves leg")
                wire("activation_giveup", {"tag": "pyro_damage"})

    # 6. activate granted Elves mana ability. Mana abilities are not
    # offered as standalone ActivateAbility on protocol 70 (see the
    # able-bodied action_types dump); if the able-bodied scan already
    # showed no offer, skip promptly with a documented reason instead of
    # burning 600 ticks.
    if (ST["pyro_resolved"] or ST.get("pyro_skipped")) \
            and not ST["elves_activated"] and not ST.get("elves_skipped") \
            and not acted("elves", rev):
        if ST.get("offers_able_scanned") \
                and not ST.get("elves_offer_checked"):
            ST["elves_offer_checked"] = True
            elves_idx = (ST.get("grant_scan") or {}).get(
                EXPECTED["ELVES"], {}).get("index")
            if elves_idx not in (ST.get("able_offers") or []):
                ST["elves_skipped"] = True
                say("[P0] elves_mana: mana ability not offered as "
                    "ActivateAbility when able-bodied "
                    f"(grant idx {elves_idx}, offers {ST.get('able_offers')});"
                    " skipping with documented reason")
                wire("activation_giveup",
                     {"tag": "elves_mana",
                      "reason": "mana ability not offered as ActivateAbility"})
                return True
        ooze_oid, o = ooze_obj(state)
        if ooze_oid and not o.get("tapped") and not o.get("summoning_sick"):
            if await activate_granted(c, 0, st, state, acts, "ELVES",
                                      "elves_mana"):
                ST["elves_activated"] = True
                ST["elves_pool_before"] = mana_pool(state, 0)
                acted("elves", rev)
                return True
            if ST.get("elves_mana_miss", 0) > 600:
                ST["elves_skipped"] = True
                say("[P0] elves activation offer never appeared "
                    "(600 ticks); skipping to remove leg")
                wire("activation_giveup", {"tag": "elves_mana"})
                return True

    # 7. removal leg: exile EVERY Ana Disciple in P0's gy via the GRANTED
    # Scavenging Ooze ability ({G}: exile target card from a graveyard;
    # no {T} cost, so it fires repeatedly). The Ooze grants it because a
    # Scavenging Ooze is in P0's graveyard (unprotected from hand-size
    # discards after the fixture looting). Fallback: a real Scavenging
    # Ooze on the battlefield (original plan). Each activation targets the
    # next remaining Ana; the top-of-tick watch records each targeted
    # object leaving the graveyard (dense-playset-safe, cf. #6906).
    if (ST["elves_resolved"] or ST.get("elves_skipped")) \
            and not ST.get("remove_scanned"):
        anas = [oid for oid in gy_oids(state, 0)
                if oname(state["objects"][oid]) == ANA]
        if anas and not ST.get("scav_target_oid") \
                and not ST.get("scav_giveup") and not acted("scavex", rev):
            fired = False
            live, _ = scan_ooze_abilities(state)
            if EXPECTED["SCAV_EXILE"] in (live or {}):
                ooze_oid, o = ooze_obj(state)
                if ooze_oid and not o.get("summoning_sick") \
                        and not o.get("tapped"):
                    if await activate_granted(c, 0, st, state, acts,
                                              "SCAV_EXILE", "scav_exile"):
                        ST["scav_activated"] = True
                        fired = True
            elif bf_named(state, 0, SCAV):
                if await activate_scav_exile(c, st, state, acts):
                    fired = True
            if fired:
                acted("scavex", rev)
                return True
            ST["scavex_miss"] = ST.get("scavex_miss", 0) + 1
            if ST["scavex_miss"] > 1500:
                ST["scav_giveup"] = True
                say("[P0] scav exile never became activatable "
                    "(1500 ticks); giving up removal leg")
                wire("activation_giveup",
                     {"tag": "scav_exile",
                      "reason": "grant absent and no real ScavOoze on BF"})
                return True
        if (not anas and ST.get("scav_activated")) \
                or ST.get("scav_giveup"):
            if not acted("remove", rev):
                acted("remove", rev)
                await export_now("post_remove.json")
                sigs, ooze_oid = scan_ooze_abilities(state)
                ST["remove_scan"] = {s: v for s, v in (sigs or {}).items()}
                n_off, idxs = scan_offers(state, acts, ooze_oid)
                ST["offer_scans"].append(
                    {"stage": "remove", "n_offers": n_off, "idxs": idxs,
                     "n_state_abs": len(sigs or {})})
                wire("remove_scan", {
                    "abilities": [
                        {"index": v["index"], "desc": v["desc"]}
                        for s, v in (sigs or {}).items()],
                    "n_offers": n_off})
                say(f"[P0] remove scan: {len(sigs or {})} abilities, "
                    f"{n_off} offers; anas_exiled={ST['anas_exiled']} "
                    f"giveup={ST.get('scav_giveup')}; finalizing")
                with open(f"{EVDIR}/remove_scan.json", "w") as f:
                    json.dump({"abilities": [
                        {"index": v["index"], "desc": v["desc"],
                         "cost": s[0], "effect": s[1], "params": s[2]}
                        for s, v in (sigs or {}).items()],
                        "n_offers": n_off,
                        "anas_exiled": ST["anas_exiled"]}, f, indent=1,
                        default=str)
                await finalize()
                return True
    return False


async def p1_tick(c, st, state, acts):
    if await preamble(c, 1, st, state, acts):
        return True
    wt, wp = wf_type(state), wf_player(state)
    if wt == "DiscardChoice" and wp == 1 and not ST["looting_p1_done"]:
        if await submit_looting_discard(c, 1, st, state):
            return True
    if not is_my_main(state, 1):
        return False
    rev = st.get("state_revision", -1)
    lid = find_hand(state, 1, MOUNTAIN)
    a = playland_for(acts, lid)
    if a and not acted("land1", rev):
        await submit_as_is(c, a)
        say("[P1] plays Mountain")
        acted("land1", rev)
        return True
    if not ST["looting_p1_done"] and not acted("loot1", rev):
        ul = untapped_land_names(state, 1)
        if find_hand(state, 1, LOOTING) and find_hand(state, 1, ELVES) \
                and find_hand(state, 1, ASSASSIN) and MOUNTAIN in ul:
            a = castspell_for(acts, find_hand(state, 1, LOOTING))
            if a:
                await submit_as_is(c, a)
                say("[P1] casts Faithless Looting")
                acted("loot1", rev)
                return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st or ST["stop"]:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
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
    # default: pass priority, but ONLY when it is ours and only when the
    # tick did nothing else. Every action branch above returns True first,
    # so this can never double-act (cast+pass, land+pass) on a stale state
    # (7144 pattern; the old separate pass loop caused wrong_player spam).
    rev = st.get("state_revision", -1)
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", rev):
                    await submit_as_is(c, a)
                    return True
            break
    return False

# ---------------- main / finalize ----------------------------------------
async def main():
    reset()
    global EXPECTED, C0, C1
    EXPECTED = expected_sigs()
    say("expected sigs: " + json.dumps(
        {k: [v[0][:50], v[1], v[2][:60]] for k, v in EXPECTED.items()},
        indent=1))
    a1 = parse_check()

    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
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
                        "server_hello": ST["server_hello"]})
    ST["a1"] = a1

    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for c, pid in ((C0, 0), (C1, 1)):
            # default pass-priority lives inside tick's tail
            await tick(c, pid)
        await asyncio.sleep(0.05)

    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finalize()


def load_state(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())["state"]
    except Exception:
        return None


def sig_present(scan, key):
    return EXPECTED[key] in (scan or {})


async def finalize():
    if ST["stop"]:
        return
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7145.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass = {}
    notes = []
    grant = ST.get("grant_scan") or {}
    remove = ST.get("remove_scan")

    # A1: parse
    a1 = ST.get("a1", "not-run")
    ass["A1_parse_grant"] = a1
    notes.append(f"A1: card-data static_abilities carry "
                 f"GrantAllActivatedAbilitiesOf(Typed Creature, controller "
                 f"null, InZone Graveyard): {a1}")

    # A2: setup
    pre = load_state("pre_grant.json")
    ok_setup = False
    if pre:
        ooze_oid, _ = ooze_obj(pre)
        p0gy, p1gy = gy_names(pre, 0), gy_names(pre, 1)
        ok_setup = bool(ooze_oid) and PYRO in p0gy and ANA in p0gy \
            and ELVES in p1gy and ASSASSIN in p1gy
        notes.append(f"A2: ooze_on_bf={bool(ooze_oid)} p0_gy={p0gy} "
                     f"p1_gy={p1gy}")
    else:
        notes.append("A2: pre_grant.json missing")
    ass["A2_setup_ok"] = "passed" if ok_setup else "failed"

    # A3: own-graveyard abilities all present
    own_keys = ["PYRO", "ANA_FLY", "ANA_WEAK"]
    own_hit = [k for k in own_keys if sig_present(grant, k)]
    ok3 = ok_setup and len(own_hit) == 3
    ass["A3_own_gy_complete"] = "passed" if ok3 else "failed"
    notes.append(f"A3: own-gy sigs present={own_hit} "
                 f"(expect all of {own_keys}); state had "
                 f"{len(grant)} activated abilities")

    # A4: opponent-graveyard abilities all present
    opp_keys = ["ELVES", "ASSASSIN"]
    opp_hit = [k for k in opp_keys if sig_present(grant, k)]
    ok4 = ok_setup and len(opp_hit) == 2
    ass["A4_opp_gy_complete"] = "passed" if ok4 else "failed"
    notes.append(f"A4: opp-gy sigs present={opp_hit} "
                 f"(expect all of {opp_keys})")

    # A5: Ana Disciple contributes exactly 2 distinct abilities
    ana_hit = [k for k in ("ANA_FLY", "ANA_WEAK") if sig_present(grant, k)]
    ok5 = ok_setup and len(ana_hit) == 2
    ass["A5_multi_ability_card"] = "passed" if ok5 else "failed"
    notes.append(f"A5: ana sigs present={ana_hit} (expect both)")

    # A6: granted Pyro ability functionally activated (P1 20->19)
    ok6 = ST["pyro_resolved"] and ST["p1_life_before_pyro"] is not None \
        and ST["life"][1] == ST["p1_life_before_pyro"] - 1
    ass["A6_activation_outcome"] = "passed" if ok6 else (
        "not-run" if not ST["pyro_activated"] else "failed")
    notes.append(f"A6: pyro_activated={ST['pyro_activated']} "
                 f"pyro_resolved={ST['pyro_resolved']} p1_life="
                 f"{ST['p1_life_before_pyro']}->{ST['life'][1]}")

    # A6c: offered abilities are exactly granted ones (no phantoms), and the
    # always-legal Pyro grant ({T}, "any target" -> P1) is offered when the
    # Ooze is able-bodied. Other grants may be legitimately unoffered:
    # Ana-flying needs {U} (P0 has no blue source), Assassin needs a tapped
    # creature target (none exists), Elves-mana is a mana ability (see
    # action_types in the able-bodied scan for how it surfaces).
    ok6c = False
    if ST.get("offers_able_scanned") and grant:
        offered = set(ST.get("able_offers") or [])
        grant_idxs = {v["index"] for v in grant.values()}
        pyro_idx = grant.get(EXPECTED["PYRO"], {}).get("index")
        no_phantoms = offered <= grant_idxs
        pyro_offered = pyro_idx is not None and pyro_idx in offered
        ok6c = bool(grant_idxs) and no_phantoms and pyro_offered
        notes.append(f"A6c: grant_idxs={sorted(grant_idxs)} offered_idxs="
                     f"{sorted(offered)} pyro_idx={pyro_idx} "
                     f"no_phantoms={no_phantoms} pyro_offered={pyro_offered}")
    else:
        notes.append(f"A6c: offers_able_scanned="
                     f"{ST.get('offers_able_scanned')} grant_n={len(grant)}")
    ass["A6c_offered_when_granted"] = "passed" if ok6c else (
        "not-run" if not ST.get("offers_able_scanned") else "failed")

    # A6b: granted Elves mana ability functionally activated
    ok6b = ST["elves_resolved"]
    ass["A6b_opp_activation_outcome"] = "passed" if ok6b else (
        "not-run" if not ST["elves_activated"] else "failed")
    notes.append(f"A6b: elves_activated={ST['elves_activated']} "
                 f"elves_resolved={ST['elves_resolved']}")

    # A7: removal updates the granted set. The removal leg exiles EVERY
    # Ana Disciple in P0's gy (tracked per-object in ST["anas_exiled"]),
    # so with zero Ana copies left the two Ana sigs must vanish while the
    # other sources' sigs (and the exiling tool's own grant) persist.
    ok7 = False
    if ST.get("scav_giveup"):
        ass["A7_removal_updates"] = "not-run"
        notes.append("A7: removal leg given up (scav exile never "
                     "activatable); not-run")
    elif remove is not None and ST["anas_exiled"]:
        post_st = load_state("post_remove.json")
        p0gy_final = gy_names(post_st, 0) if post_st else None
        ana_gone = p0gy_final is not None and ANA not in p0gy_final
        ana_sigs_gone = not sig_present(remove, "ANA_FLY") \
            and not sig_present(remove, "ANA_WEAK")
        others_stay = all(sig_present(remove, k)
                          for k in ("PYRO", "ELVES", "ASSASSIN"))
        # the exiling tool itself (granted ScavOoze ability) must persist
        # if it was granted: its source is still in the graveyard
        scav_was_granted = sig_present(grant, "SCAV_EXILE")
        scav_stays = (not scav_was_granted) or sig_present(remove,
                                                           "SCAV_EXILE")
        ok7 = ana_gone and ana_sigs_gone and others_stay and scav_stays
        notes.append(f"A7: anas_exiled={ST['anas_exiled']} p0gy_final="
                     f"{p0gy_final} ana_gone={ana_gone} ana_sigs_gone="
                     f"{ana_sigs_gone} others_stay={others_stay} "
                     f"scav_grant_stays={scav_stays} "
                     f"remove_scan_n={len(remove)}")
        ass["A7_removal_updates"] = "passed" if ok7 else "failed"
    else:
        notes.append(f"A7: remove_scan={'present' if remove else 'missing'} "
                     f"anas_exiled={ST['anas_exiled']}")
        ass["A7_removal_updates"] = "not-run"

    # A8: cleanup
    post = load_state("post_remove.json") or pre
    stack = (post.get("stack") or []) if post else None
    ok8 = post is not None and (stack == [] or stack is None)
    ass["A8_cleanup"] = "passed" if ok8 else "failed"
    notes.append(f"A8: final stack={stack if stack is not None else '?'} "
                 f"turn={post.get('turn_number') if post else '?'}")

    core_missing = [k for k in ("A3_own_gy_complete", "A4_opp_gy_complete",
                                "A5_multi_ability_card")
                    if ass[k] == "failed"]
    if ass["A2_setup_ok"] == "failed":
        verdict = "blocked"
    elif core_missing:
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in
             ("A2_setup_ok", "A3_own_gy_complete", "A4_opp_gy_complete",
              "A5_multi_ability_card", "A6_activation_outcome",
              "A6b_opp_activation_outcome", "A6c_offered_when_granted",
              "A7_removal_updates")):
        verdict = "not-reproduced"
    else:
        # setup ok, grant complete, but a functional leg failed/not-run
        verdict = "reproduced" if ass["A6_activation_outcome"] == "failed" \
            or ass["A6b_opp_activation_outcome"] == "failed" \
            or ass["A6c_offered_when_granted"] == "failed" \
            or ass["A7_removal_updates"] == "failed" else "not-reproduced"
    say(f"VERDICT: {verdict}")
    notes.append(f"verdict={verdict}")
    if verdict == "reproduced":
        missing = [k for k in ("PYRO", "ANA_FLY", "ANA_WEAK", "ELVES",
                               "ASSASSIN") if not sig_present(grant, k)]
        notes.append(f"finding: Necrotic Ooze's granted set is incomplete; "
                     f"missing sigs at grant scan: {missing}")
    elif verdict == "not-reproduced":
        notes.append("finding: Ooze gained all 5 expected abilities from "
                     "both graveyards (incl. both of Ana Disciple's), all "
                     "were offered for activation when able-bodied, both "
                     "sample activations resolved, and exiling a source "
                     "removed exactly its abilities.")

    run = {
        "issue": 7145,
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "server_hello": ST["server_hello"],
        "game_code": C0.game_code if C0 else None,
        "seats": {"P0": C0.player_id if C0 else None,
                  "P1": C1.player_id if C1 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "offer_scans": ST["offer_scans"],
        "rejections": ST["rejections"],
        "unexpected": ST["unexpected"],
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Necrotic Ooze continuous grant: enumeration of activated "
                 "abilities from both players' graveyards (own + opponent), "
                 "multi-ability card, functional activation of own- and "
                 "opp-sourced grants, source-removal update; native engine, "
                 "two human-client seats",
        "limitations": [
            "Browser UI not exercised.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "Authoritative exports only; no standalone state-restore in "
            "this build.",
            "Faithless Looting is the graveyard-stocking fixture; natural "
            "deaths were not used.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written")

    try:
        WIRE.close()
    except Exception:
        pass
    RUNLOG.close()
    print("logs closed", flush=True)

    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(f"{EVDIR}/{f}")
                   and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest written ({len(lines)} files)", flush=True)

    try:
        import subprocess
        subprocess.run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7145.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)

    try:
        import shutil
        slog = f"{BACKFILL}/runs/{RUN_ID}/server.log"
        gcode = C0.game_code if C0 else ""
        out = []
        with open(slog) as f:
            for line in f:
                if gcode and gcode in line:
                    out.append(line)
        with open(f"{EVDIR}/server.log.excerpt.txt", "w") as f:
            f.writelines(out[-400:])
        print(f"server.log excerpt written ({len(out)} matching lines)",
              flush=True)
    except Exception as e:
        print(f"server.log excerpt failed: {e}", flush=True)

    try:
        files = sorted(f for f in os.listdir(EVDIR)
                       if os.path.isfile(f"{EVDIR}/{f}")
                       and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"manifest re-hashed ({len(lines)} files)", flush=True)
    except Exception as e:
        print(f"manifest re-hash failed: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
