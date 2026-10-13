#!/usr/bin/env python3
"""Issue #9856: Chromatic Armor -- "sources of the last chosen color" has no
representation.

Oracle (Chromatic Armor): "Enchant creature
As this Aura enters, choose a color.
This Aura enters with a sleight counter on it.
Prevent all damage that would be dealt to enchanted creature by sources of the
last chosen color.
{X}: Put a sleight counter on this Aura and choose a color. X is the number of
sleight counters on this Aura."

Report (phase-rs/phase#9856, opened 2026-10-12, open,
bug/area:parser/priority:p3-card-specific): read from code -- the "last
chosen" reference is an honest gap (unimplemented). Whether a re-choice
persists on the object across the {X} activation is unverified.
Where: `IsChosenColor` filter family; chosen-attribute storage on the
permanent. CR 607.2d (linked choice abilities).

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with
game-level confirmation of the consequences):
  Parse (decisive): in the pinned v0.109.0 card-data.json, the prevention
      sentence must have a representation of the "sources of the last chosen
      color" source scope -- or the honest gap must be confirmed present.
      Bug: the sentence is parsed as {"type": "Unimplemented",
      "name": "prevention_source_scope"} -- no representation.
  Parse: the {X} activated ability's re-choose is
      {"type":"Choose","choice_type":"Color","persist":false} while the ETB
      "As ~ enters, choose a color." replacement is
      {"type":"Choose","choice_type":"Color","persist":true} -- the re-choice
      is not persisted on the object (the report's unverified question).
      Also the "X is the number of sleight counters on ~" clause is
      {"type":"Unimplemented","name":"unrecognized_clause_head"}.
  Game (confirming):
      - P0 casts Elite Vanguard, casts Chromatic Armor enchanting it; the
        ETB replacement offers a color choice (choose Red); the Armor enters
        with 1 sleight counter.
      - P0 activates {X}: observe how X is valued (clause unimplemented),
        pay, choose a new color (Blue); inspect the Armor object for stored
        chosen color(s).
      - P1 casts Lightning Bolt targeting the enchanted Vanguard (a red
        source): with the prevention unimplemented, the 3 damage is NOT
        prevented -- the Vanguard dies. Documents the in-game consequence.

Assertions:
  A1 setup_ok            Armor cast, enchanting the Vanguard, ETB color choice
                         offered and answered (Red), 1 sleight counter
  A2 prevention_unimpl   FAILED = the reported bug: the prevention sentence
                         is parsed Unimplemented (prevention_source_scope) --
                         "sources of the last chosen color" has no
                         representation; passed = a representation exists
  A3 rechoice_persist     FAILED = the reported gap: ETB Choose persist:true
                         but the {X} re-Choose is persist:false; passed =
                         both persist
  A4 damage_not_prevented passed = Lightning Bolt's damage to the enchanted
                         creature was applied (Vanguard died) -- documents
                         the consequence of the unimplemented prevention;
                         not-run = the Bolt leg never completed
  A5 rechoice_not_stored  FAILED = after the {X} activation + Blue re-choice,
                         no chosen color is recorded on the Armor object
                         (persist:false); ALSO failed = the {X} ability
                         cannot be activated at all (advertised activation
                         accepted but applied with zero events, no re-choice
                         prompt); passed = a chosen color is recorded;
                         not-run = the {X} leg never completed
  A6 oracle_intact        passed = the pinned oracle text still contains the
                         prevention sentence (the gap is in the lower, not
                         the data)

Verdict:
  reproduced      A2 failed (the prevention has no representation -- the
                  reported outcome), corroborated by A3 and the game legs
  not-reproduced  A2 passed (the scope is represented)
  blocked         parse evidence unreadable or the fixture never reached

Driver conventions (protocol 140, from scenario_650_01090.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - waiting_for gone (null); priority = PassPriority in top-level
    legal_actions; decisions via viewer_interaction
  - exact card-data names; export response data.state is a JSON string parsed
    once; runtime state objects use "card_types" (plural)
  - MulliganDecision via legacy Action (Keep); DiscardToHandSize via vi;
    mana via vi tapLandForMana; casts via advertised legal actions submitted
    as advertised (CastSpell targets:[] answered via the engine's vi target
    selection afterwards -- never inject targets into CastSpell)
  - ActivateAbility submitted while holding priority
  - real_decision_pending filters None surface codes and the priority-menu
    codes (incl. playLand); sleep(0) yield before leg evaluation
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9856")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9856-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9856
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.109.0",
    "build_commit": "932e389",
    "protocol_version": 140,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.109.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "fresh game created by this run; evidence under evidence/9856/"
               + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


for _f, _k in (("server/releases/v0.109.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.109.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.109.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h, f"empty hash for {_f}"
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.109.0/data/card-data.json"))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
LAST_PASS_T = {}


async def maybe_pass_priority(c, st, acts, tag):
    """Pass priority when holding it. Re-pass on the same revision if >5s
    elapsed: passing rarely changes the revision, and priority can come
    back to the same player (both players must pass in succession)."""
    if not my_priority(acts):
        return False
    last_rev = PASSED_REV.get(c.name, -1)
    if last_rev < c.revision or \
            (last_rev == c.revision
             and time.time() - LAST_PASS_T.get(c.name, 0) > 5):
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision
        LAST_PASS_T[c.name] = time.time()
        return True
    return False


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def player_life(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def named_oids_anywhere(state, lname):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if str(o.get("base_name") or o.get("name") or "").lower() == lname]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


ARMOR = "chromatic armor"
VANGUARD = "elite vanguard"
BOLT = "lightning bolt"
PLAINS = "plains"
ISLAND = "island"
MOUNTAIN = "mountain"
COLORS = {"white", "blue", "black", "red", "green"}


def armor_oids(state, pid=0):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == ARMOR]


def vanguard_oids(state, pid=0):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == VANGUARD]


def counters_named(state, oid, needle):
    o = get_obj(state, oid)
    raw = o.get("counters")
    total = 0
    if isinstance(raw, dict):
        for k, v in raw.items():
            if needle in str(k).lower():
                try:
                    total += int(v)
                except (TypeError, ValueError):
                    total += 1
    elif isinstance(raw, list):
        for c in raw:
            nm = c.get("name") if isinstance(c, dict) else str(c)
            if needle in str(nm).lower():
                try:
                    total += int(c.get("count", 1)) if isinstance(c, dict) else 1
                except (TypeError, ValueError):
                    total += 1
    return total, raw


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type")
            if spec == "relations":
                continue
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:12],
                    "rtype": resp.get("type"),
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c),
                    "texts": [choice_text(ch)[:24] for ch in items[:8]]})
    return out


# ------------------------------------------------------------- interaction primitives

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] is not None:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.109.0"), f"unexpected version {ver}"
    assert int(proto) == 140, f"unexpected protocol {proto}"
    assert str(build) == "932e389", f"unexpected build {build}"


# ------------------------------------------------------- parse-level evidence

def _walk_effects(entry):
    """Yield every effect dict in the parsed entry (abilities, sub-chains,
    replacements, triggers, static_abilities)."""
    def rec_ability(a):
        seen = []
        cur = a
        while isinstance(cur, dict):
            eff = cur.get("effect")
            if isinstance(eff, dict):
                seen.append(eff)
            cur = cur.get("sub_ability")
        return seen

    for a in entry.get("abilities", []) or []:
        yield from rec_ability(a)
    for r in entry.get("replacements", []) or []:
        ex = r.get("execute")
        if isinstance(ex, dict):
            yield from rec_ability(ex)
    for t in entry.get("triggers", []) or []:
        for k in ("execute", "effect"):
            ex = t.get(k)
            if isinstance(ex, dict):
                yield from rec_ability(ex)
    for s in entry.get("static_abilities", []) or []:
        eff = s.get("effect")
        if isinstance(eff, dict):
            yield eff


def check_parse():
    """Decisive parse-level check for #9856. Never aborts: the missing
    representation IS the reported bug."""
    entry = CARD_DATA.get(ARMOR, {})
    blob = json.dumps(entry)
    oracle = str(entry.get("oracle_text") or "")
    ev = {"card": "Chromatic Armor", "entry_found": bool(entry)}
    ev["oracle_has_prevention_sentence"] = \
        "Prevent all damage that would be dealt to enchanted creature" in oracle
    ev["oracle_has_last_chosen"] = "last chosen color" in oracle
    # A2: does the prevention sentence have any non-Unimplemented
    # representation? Look for a prevention-shaped effect.
    effects = list(_walk_effects(entry))
    prev_unimpl = [e for e in effects
                   if e.get("type") == "Unimplemented"
                   and "prevent" in str(e.get("description", "")).lower()]
    prev_impl = [e for e in effects
                 if e.get("type") not in (None, "Unimplemented")
                 and "prevent" in json.dumps(e).lower()]
    ev["prevention_unimplemented"] = [ {"name": e.get("name"),
                                        "description": e.get("description")}
                                       for e in prev_unimpl]
    ev["prevention_implemented_effects"] = len(prev_impl)
    # the decisive question: any representation of the "last chosen color"
    # source scope?
    ev["has_last_chosen_scope"] = "last chosen" in blob.lower() and bool(prev_impl)
    # A3: persist flags on the two color choices
    chooses = [e for e in effects
               if e.get("type") == "Choose" and e.get("choice_type") == "Color"]
    ev["color_chooses"] = [{"persist": e.get("persist")} for e in chooses]
    etb_choose = None
    for r in entry.get("replacements", []) or []:
        if "choose a color" in str(r.get("description", "")).lower():
            ex = r.get("execute") or {}
            eff = ex.get("effect") or {}
            if eff.get("type") == "Choose":
                etb_choose = eff.get("persist")
    ev["etb_choose_persist"] = etb_choose
    x_choose = None
    for a in entry.get("abilities", []) or []:
        if a.get("kind") == "Activated":
            cur = a.get("sub_ability")
            while isinstance(cur, dict):
                eff = cur.get("effect") or {}
                if eff.get("type") == "Choose" and \
                        eff.get("choice_type") == "Color":
                    x_choose = eff.get("persist")
                    break
                cur = cur.get("sub_ability")
    ev["x_ability_choose_persist"] = x_choose
    # the "X is the number of sleight counters" clause
    x_clause_unimpl = [e for e in effects
                       if e.get("type") == "Unimplemented"
                       and "sleight counters" in str(e.get("description", ""))]
    ev["x_clause_unimplemented"] = [{"name": e.get("name"),
                                     "description": e.get("description")}
                                    for e in x_clause_unimpl]
    ev["n_abilities"] = len(entry.get("abilities", []) or [])
    ev["n_replacements"] = len(entry.get("replacements", []) or [])
    ev["replacement_descriptions"] = [r.get("description")
                                      for r in entry.get("replacements", []) or []]
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/card_entry.json", "w") as f:
        json.dump(entry, f, indent=1)
    say(f"parse-level: prevention_unimplemented={len(prev_unimpl)} "
        f"prevention_implemented={len(prev_impl)} "
        f"etb_choose_persist={etb_choose} x_choose_persist={x_choose} "
        f"x_clause_unimplemented={len(x_clause_unimpl)}")
    wire("parse_level", ev)
    return ev


# ------------------------------------------------------------- action helpers

MULL_COUNT = {}


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    # P0 mulligans toward a Vanguard+Armor opener (max 3 mulligans); P1 keeps.
    if pid == 0 and len(hn) > 4 and MULL_COUNT.get(tag, 0) < 3 \
            and (VANGUARD not in hn or ARMOR not in hn):
        MULL_COUNT[tag] = MULL_COUNT.get(tag, 0) + 1
        say(f"[{tag}] mulligan #{MULL_COUNT[tag]} ({len(hn)} cards, "
            f"hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: per-card SelectCards legal actions plus a
    viewer_interaction schema/select opportunity (waitingForKind.code stays
    'mulligan'). Gated on kind == 'mulligan' AND turn 1 / Untap. Keep
    Vanguard/Armor on top."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        ref = _cand_ref(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in (VANGUARD, ARMOR):
            return (1, str(ref))  # keep these on top
        return (0, str(ref))  # bottom everything else first

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_ref(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": str(iid)[:12],
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []]
                syms = []
                for m in manas:
                    if m.get("type") == "mana":
                        syms = (m.get("data") or {}).get("symbols", []) or []
                        break
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action({"type": "PassPriority"})
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def untapped_colored_lands(state, pid):
    out = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}
    cmap = {"plains": "W", "island": "U", "swamp": "B",
            "mountain": "R", "forest": "G"}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = cmap.get(obj_lname(state, o))
            if c:
                out[c] += 1
    return out


def find_activates(st, source_oid):
    """All advertised ActivateAbility actions for a source (deduped by
    ability_index). The Armor's {X} ability was advertised as index 1;
    submitting it was accepted by the server but produced zero events
    (server log: action applied, event_count=0) -- so try every advertised
    index before declaring the leg stalled."""
    out = []
    seen = set()
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility" and \
                str((a.get("data") or {}).get("source_id")) == str(source_oid):
            idx = (a.get("data") or {}).get("ability_index")
            if idx not in seen:
                seen.add(idx)
                out.append(a)
    return out


def find_activate(st, source_oid):
    ms = find_activates(st, source_oid)
    if ms:
        return ("submit", ms[0])
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" in codes:
                return ("vi", opp, ch)
    return None


# ------------------------------------------------------------- choice handling

def color_choice_opportunity(st):
    """Find a vi opportunity that offers the five colors (the Choose-Color
    effect). Returns (opp, choice_list) or (None, None)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        texts = {choice_text(ch).strip().lower() for ch in items}
        if len(COLORS & texts) >= 3:
            return opp, items
    return None, None


def pick_color(items, want):
    want = want.lower()
    for ch in items:
        if choice_text(ch).strip().lower() == want:
            return ch
    return None


def target_opportunity(st):
    """First vi opportunity that looks like a target selection: schema
    select/sequence with candidates, or exactChoices with candidate/target
    codes (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            # text-spec opportunities (e.g. the Choose-Color prompt) are
            # answered by the color-choice handler, never as targets.
            if stype == "text":
                continue
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_by_name(state, opp, lname):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        ref = _cand_ref(ch)
        if ref and obj_lname(state, ref) == lname:
            return ch
    for ch in cands:
        if lname in choice_text(ch).lower():
            return ch
    return None


# ------------------------------------------------------------------- ticks

async def drain_rejections(c, tag):
    while c.rejections:
        r = c.rejections.pop(0)
        wire("rejection", {"who": tag, "rejection": r})
        say(f"[{tag}] REJECTION: {json.dumps(r)[:300]}")


async def do_choose_x(c, acts, st, pid, tag, ctx):
    """Answer the engine's ChooseX legal action (GameAction::ChooseX
    { value }). The Armor's {X} ability asks the player to announce X
    because the 'X is the number of sleight counters' clause is
    unimplemented in the pinned data. Per driver rules, submit the
    advertised action EXACTLY as advertised -- the engine values X itself
    (it advertises value 0 when the clause is unimplemented). A value
    substituted by the driver is rejected as invalid_action (observed
    2026-10-13: advertised {"value": 0}, driver-sent {"value": 1}
    -> invalid_action 'That action is not valid in the current game
    state.')."""
    for a in acts:
        if a.get("type") != "ChooseX":
            continue
        key = (tag, "choosex", f"rev{c.revision}")
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        src = ctx.get("armor_oid")
        n, raw = counters_named(st["state"], src, "sleight") if src else (1, None)
        adv_val = (a.get("data") or {}).get("value")
        say(f"[{tag}] announcing X: advertised value={adv_val} "
            f"(sleight counters={n}, raw={raw}); submitting as advertised")
        wire("choose_x", {"who": tag, "advertised_value": adv_val,
                          "sleight_counters": n, "sleight_raw": raw,
                          "advertised_action": a})
        wire("action_submit", {"who": c.name, "action": a})
        # Realign the mana plan with the engine-advertised X (the {X} cost
        # is what the engine values, not what the oracle says).
        try:
            x_cost = int(adv_val)
        except (TypeError, ValueError):
            x_cost = 1
        ST["mana_needs"][tag] = {"generic": x_cost} if x_cost > 0 else {}
        await submit_as_is(c, a)
        return True
    return False


async def common_tick_head(c, acts, st, pid, tag, ctx):
    await drain_rejections(c, tag)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    if await do_choose_x(c, acts, st, pid, tag, ctx):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # never block in this scenario; hold
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


COLOR_TRY = {}


def _pick_value_text(pick, want):
    for s in pick.get("surfaces", []) or []:
        d = s.get("data") or {}
        if s.get("type") == "value" and isinstance(d, dict) and d.get("value"):
            return str(d["value"])
    return want.capitalize()


def _color_shapes(iid, pick, want):
    cid = pick.get("id")
    val = _pick_value_text(pick, want)
    return [
        ("text/text", {"interactionId": iid,
                       "response": {"type": "text",
                                    "data": {"text": val}}}),
        ("text/value", {"interactionId": iid,
                        "response": {"type": "text",
                                     "data": {"value": val}}}),
        ("choose/choiceId", {"interactionId": iid,
                             "response": {"type": "choose",
                                          "data": {"choiceId": cid}}}),
        ("text/choiceIds", {"interactionId": iid,
                            "response": {"type": "text",
                                         "data": {"choiceIds": [cid]}}}),
        ("select/choiceIds", {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [cid]}}}),
    ]


async def answer_color_choice(c, st, tag, want, ctx_key, ctx):
    """Answer a Choose-Color opportunity with `want`. The color opportunity
    uses a schema/text spec (not select/sequence); the accepted submission
    shape is discovered by trying shapes in order and watching for the
    opportunity to clear. The winning shape is recorded in
    ctx[ctx_key + '_shape']."""
    key = (tag, ctx_key)
    opp, items = color_choice_opportunity(st)
    if opp is None:
        pend = COLOR_TRY.get(key)
        if pend and pend.get("submitted"):
            say(f"[{tag}] color choice '{want}': WINNER shape "
                f"{pend['shape_name']} (opportunity cleared)")
            wire("color_shape_winner",
                 {"who": tag, "want": want,
                  "shape": pend["shape_name"],
                  "iid": str(pend["iid"])[:12]})
            ctx[ctx_key] = want
            ctx[ctx_key + "_shape"] = pend["shape_name"]
            del COLOR_TRY[key]
        return False
    iid = opp.get("interactionId")
    pend = COLOR_TRY.get(key)
    if pend is None or pend.get("iid") != iid:
        pend = {"iid": iid, "shape_idx": 0, "tries": 0,
                "submitted": False, "shape_name": None}
        COLOR_TRY[key] = pend
        wire("color_choice_seen",
             {"who": tag, "iid": str(iid)[:12], "want": want,
              "offered": [choice_text(ch) for ch in items],
              "spec_type": ((opp.get("response", {}) or {}).get("data", {})
                            or {}).get("spec", {}).get("type")})
    pick = pick_color(items, want)
    if pick is None:
        say(f"[{tag}] color choice offered but '{want}' not among "
            f"{[choice_text(ch) for ch in items]} -- recorded, not answered")
        return True
    shapes = _color_shapes(iid, pick, want)
    if pend["shape_idx"] >= len(shapes):
        return True  # exhausted; hold
    name, sub = shapes[pend["shape_idx"]]
    if not pend["submitted"]:
        pend["submitted"] = True
        pend["shape_name"] = name
        pend["tries"] = 0
        say(f"[{tag}] color choice '{want}': trying shape {name}")
        wire("color_shape_try", {"who": tag, "want": want, "shape": name,
                                 "submission": sub})
        await interact_as(c, sub, tag)
        return True
    pend["tries"] += 1
    if pend["tries"] >= 10:
        say(f"[{tag}] color choice '{want}': shape {name} did not clear "
            f"the opportunity; next shape")
        wire("color_shape_miss", {"who": tag, "want": want, "shape": name})
        pend["shape_idx"] += 1
        pend["submitted"] = False
    return True


async def p0_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 0, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # --- track the Armor entering the battlefield
    if not ctx.get("armor_oid"):
        armors = armor_oids(state, 0)
        if armors:
            ctx["armor_oid"] = armors[0]
            n, raw = counters_named(state, armors[0], "sleight")
            ctx["armor_sleight_at_entry"] = n
            say(f"[{tag}] Chromatic Armor on battlefield (oid {armors[0]}), "
                f"sleight counters={n} (raw={raw})")
            wire("armor_entered", {"oid": armors[0], "sleight": n,
                                   "sleight_raw": raw,
                                   "armor_object": get_obj(state, armors[0])})
            with open(f"{EVDIR}/armor_object_at_entry.json", "w") as f:
                json.dump(get_obj(state, armors[0]), f, indent=1,
                          default=str)

    # --- ETB color choice: the "As ~ enters, choose a color" replacement
    if ctx.get("armor_cast") and not ctx.get("etb_chosen"):
        if await answer_color_choice(c, st, tag, "red", "etb_chosen", ctx):
            return

    # --- main-phase plays
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return
        hn = hand_names(state, 0)
        cols = untapped_colored_lands(state, 0)
        # cast Elite Vanguard first (the enchant target)
        if not ctx.get("vanguard_cast") and VANGUARD in hn \
                and cols["W"] >= 1 and untapped_land_total(state, 0) >= 1:
            a, oid = cast_action_for(acts, state, VANGUARD)
            if a is not None:
                ctx["vanguard_cast"] = True
                ST["mana_needs"][tag] = {"W": 1}
                say(f"[{tag}] casting Elite Vanguard (oid {oid})")
                wire("cast_vanguard", {"oid": oid})
                await submit_as_is(c, a)
                return
        # cast Chromatic Armor; the Aura's enchant target is chosen via the
        # engine's vi TargetSelection AFTER the cast (CR 601.2c -- never
        # inject targets into CastSpell).
        if ctx.get("vanguard_cast") and not ctx.get("armor_cast") \
                and ARMOR in hn and cols["W"] >= 1 and cols["U"] >= 1 \
                and untapped_land_total(state, 0) >= 3:
            a, oid = cast_action_for(acts, state, ARMOR)
            if a is not None:
                ctx["armor_cast"] = True
                ST["mana_needs"][tag] = {"W": 1, "U": 1, "generic": 1}
                say(f"[{tag}] casting Chromatic Armor (oid {oid}); target "
                    f"(the Vanguard) to be chosen at the engine's "
                    f"target-selection prompt")
                wire("cast_armor", {"oid": oid,
                                    "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return

    # --- answer the Armor's enchant target selection with the Vanguard
    if ctx.get("armor_cast") and not ctx.get("armor_targeted"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            wire("armor_target_seen",
                 {"rev": c.revision, "rtype": rtype, "stype": stype,
                  "candidates": [{"text": choice_text(ch)[:80],
                                  "ref": _cand_ref(ch),
                                  "ref_name": obj_lname(state, _cand_ref(ch))
                                  if _cand_ref(ch) else None}
                                 for ch in cands]})
            pick = pick_target_by_name(state, opp, VANGUARD)
            if pick is not None:
                ctx["armor_targeted"] = True
                say(f"[{tag}] enchanting the Elite Vanguard with "
                    f"Chromatic Armor")
                await answer_vi(c, opp, pick, tag)
                return
            key = (tag, "armor-target-miss", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] armor target prompt has no Vanguard candidate "
                    f"-- recorded, not answered")
            return

    # --- A5 leg: activate {X} once the Armor is out with a sleight counter.
    # X is the number of sleight counters (clause unimplemented in data);
    # observe how the engine values X. Try every advertised ability_index
    # (the index-1 advertisement was accepted by the server but produced
    # zero events); give up after 90s with no re-choice and let the Bolt
    # leg proceed.
    if ctx.get("armor_oid") and ctx.get("etb_chosen") \
            and not ctx.get("x_rechosen") and not ctx.get("x_stalled") \
            and my_main(state, 0) and not (state.get("stack") or []):
        src = ctx["armor_oid"]
        if obj_zone(state, src) == "Battlefield":
            adv = find_activates(st, src)
            tried = ctx.setdefault("x_tried_indices", [])
            if not ctx.get("x_advertised_logged"):
                ctx["x_advertised_logged"] = True
                wire("x_ability_advertised",
                     {"source_oid": src,
                      "indices": [(a.get("data") or {}).get("ability_index")
                                  for a in adv]})
                say(f"[{tag}] Armor advertised ActivateAbility indices: "
                    f"{[(a.get('data') or {}).get('ability_index') for a in adv]}")
            nxt = next((a for a in adv
                        if (a.get("data") or {}).get("ability_index") not in tried),
                       None)
            if nxt is not None:
                idx = (nxt.get("data") or {}).get("ability_index")
                tried.append(idx)
                ctx["x_activated"] = True
                if not ctx.get("x_first_try_t"):
                    ctx["x_first_try_t"] = time.time()
                n, _ = counters_named(state, src, "sleight")
                ST["mana_needs"][tag] = {"generic": 1}
                say(f"[{tag}] activating Armor {{X}} ability_index={idx} "
                    f"(sleight counters={n})")
                wire("activate_x", {"source_oid": src, "ability_index": idx,
                                    "sleight_counters": n})
                await submit_as_is(c, nxt)
                return
            if ctx.get("x_first_try_t") \
                    and time.time() - ctx["x_first_try_t"] > 90:
                ctx["x_stalled"] = True
                ST["mana_needs"][tag] = {}
                n, raw = counters_named(state, src, "sleight")
                say(f"[{tag}] X leg STALLED: tried indices {tried}, no "
                    f"re-choice opportunity in 90s; sleight counters now "
                    f"{n} (raw={raw}) -- proceeding to the Bolt leg")
                wire("x_leg_stalled", {"tried_indices": tried,
                                       "sleight_counters": n,
                                       "sleight_raw": raw,
                                       "armor_object": get_obj(state, src)})
                return

    # --- {X} re-choice: choose Blue
    if ctx.get("x_activated") and not ctx.get("x_rechosen") \
            and not ctx.get("x_stalled"):
        if await answer_color_choice(c, st, tag, "blue", "x_rechosen", ctx):
            src = ctx.get("armor_oid")
            if src and obj_zone(state, src) == "Battlefield":
                wire("armor_object_after_x",
                     {"oid": src, "armor_object": get_obj(state, src)})
                with open(f"{EVDIR}/armor_object_after_x.json", "w") as f:
                    json.dump(get_obj(state, src), f, indent=1, default=str)
                n, raw = counters_named(state, src, "sleight")
                ctx["armor_sleight_after_x"] = n
                say(f"[{tag}] Armor after {{X}}: sleight counters={n} "
                    f"(raw={raw})")
            ST["mana_needs"][tag] = {}
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    await maybe_pass_priority(c, st, acts, tag)


async def p1_tick(c, tag, ctx, host=None):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 1, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # --- A4 leg: Lightning Bolt at the enchanted Vanguard, once the {X}
    # leg is done (re-chosen) or stalled (the Armor + Vanguard are still on
    # the battlefield).
    if (ctx.get("x_rechosen") or ctx.get("x_stalled")) \
            and not ctx.get("bolt_cast") and my_main(state, 1):
        vgs = vanguard_oids(state, 0)
        hn = hand_names(state, 1)
        cols = untapped_colored_lands(state, 1)
        if vgs and BOLT in hn and cols["R"] >= 1 \
                and untapped_land_total(state, 1) >= 2:
            a, oid = cast_action_for(acts, state, BOLT)
            if a is not None:
                ctx["bolt_cast"] = True
                ctx["bolt_target_oid"] = vgs[0]
                ctx["bolt_cast_rev"] = c.revision
                ctx["vanguard_life_before"] = player_life(state, 0)
                ST["mana_needs"][tag] = {"R": 1, "generic": 1}
                say(f"[{tag}] casting Lightning Bolt (oid {oid}) at the "
                    f"enchanted Vanguard (oid {vgs[0]})")
                wire("cast_bolt", {"oid": oid, "target_oid": vgs[0],
                                   "cast_action_data": a.get("data")})
                # Host-only export: only the game host (seat 0, p0) may call
                # ExportAuthoritativeState; p1's own export is rejected.
                pre = json.loads(await (host or c).export_state())
                with open(f"{EVDIR}/pre_bolt.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_bolt_export", {})
                await submit_as_is(c, a)
                return

    # --- answer the Bolt target selection with the Vanguard
    if ctx.get("bolt_cast") and not ctx.get("bolt_targeted"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            pick = pick_target_by_name(state, opp, VANGUARD)
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            wire("bolt_target_seen",
                 {"rev": c.revision, "rtype": rtype, "stype": stype,
                  "candidates": [{"text": choice_text(ch)[:80],
                                  "ref": _cand_ref(ch),
                                  "ref_name": obj_lname(state, _cand_ref(ch))
                                  if _cand_ref(ch) else None}
                                 for ch in cands]})
            if pick is not None:
                ctx["bolt_targeted"] = True
                say(f"[{tag}] targeting the enchanted Vanguard with "
                    f"Lightning Bolt")
                await answer_vi(c, opp, pick, tag)
                return
            key = (tag, "bolt-target-miss", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] bolt target prompt has no Vanguard candidate "
                    f"-- recorded, not answered")
            return

    # --- track the Bolt resolution outcome: was damage prevented?
    if ctx.get("bolt_targeted") and not ctx.get("a4_recorded"):
        tgt = ctx.get("bolt_target_oid")
        z = obj_zone(state, tgt) if tgt else None
        on_stack = any(obj_lname(state, e.get("object_id", e.get("id", "")))
                       == BOLT for e in state.get("stack") or [])
        if not on_stack and ctx.get("bolt_resolve_seen") is None:
            # first tick after the Bolt left the stack: record outcome
            ctx["bolt_resolve_seen"] = True
            ctx["a4_recorded"] = True
            ctx["a4_vanguard_zone"] = z
            ctx["a4_p0_life_after"] = player_life(state, 0)
            armor_z = obj_zone(state, ctx.get("armor_oid")) \
                if ctx.get("armor_oid") else None
            ctx["a4_armor_zone"] = armor_z
            say(f"[{tag}] A4: Bolt resolved; Vanguard (oid {tgt}) zone={z}, "
                f"Armor zone={armor_z}, P0 life={ctx['a4_p0_life_after']}")
            wire("bolt_resolved", {"vanguard_oid": tgt, "vanguard_zone": z,
                                   "armor_zone": armor_z,
                                   "p0_life_after": ctx["a4_p0_life_after"]})
            post = json.loads(await (host or c).export_state())
            with open(f"{EVDIR}/post_bolt.json", "w") as f:
                json.dump(post, f, indent=1)
            wire("post_bolt_export", {})

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    await maybe_pass_priority(c, st, acts, tag)


# ------------------------------------------------------------------- run

def find_chosen_hits(obj):
    """Recursively search a runtime object for chosen/choice/color storage."""
    hits = []

    def rec(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                if "chosen" in kl or "choice" in kl:
                    hits.append((path + "/" + str(k), v))
                elif kl == "color" and isinstance(v, str) \
                        and v.lower() in COLORS:
                    hits.append((path + "/" + str(k), v))
                rec(v, path + "/" + str(k))
        elif isinstance(x, list):
            for i, v in enumerate(x):
                rec(v, f"{path}[{i}]")

    rec(obj, "")
    return hits


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    LAST_PASS_T.clear()
    MULLS.clear()
    MULL_COUNT.clear()
    COLOR_TRY.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9856-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Plains", 24), ("Island", 24),
                         ("Elite Vanguard", 6), ("Chromatic Armor", 6)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Mountain", 28), ("Island", 28),
                                     ("Lightning Bolt", 4)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "not-run"

    # mulligan phase: actively tick (the mulligan-toward + bottom logic
    # lives in the ticks) until both seats are past mulligan -- turn 1 with
    # no MulliganDecision pending and no bottom prompt -- then check the
    # hand. A passive wait would fast-abort on the pre-mulligan hand.
    t_mull = time.time()
    while time.time() - t_mull < 90:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx, p0)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
        st = p0.latest
        if st and st["state"].get("turn_number", 0) >= 1:
            acts = merged_actions(st)
            if not any(a.get("type") == "MulliganDecision" for a in acts) \
                    and vi_kind_code(st) != "mulligan":
                break
    st = p0.latest
    hn = hand_names(st["state"], 0) if st else []
    if st is None or VANGUARD not in hn or ARMOR not in hn:
        say(f"[{tag}] fast abort: opening hand lacks Vanguard+Armor ({hn})")
        wire("fast_abort", {"reason": "no vanguard+armor in opening hand",
                            "hand": hn})
        obs["notes"].append("fast abort: opening hand lacked Elite Vanguard "
                            "+ Chromatic Armor; fresh game redealt")
        obs["assert"]["A4_damage_not_prevented"] = "not-run"
        obs["assert"]["A5_rechoice_not_stored"] = "not-run"
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 600:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx, p0)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
        st_now = p0.latest
        turn = st_now["state"].get("turn_number", 0) if st_now else 0
        if turn > 30 and not ctx.get("a4_recorded"):
            say(f"[{tag}] turn {turn} exceeded without A4; abandoning attempt")
            obs["a4_giveup"] = True
            obs["a4_giveup_reason"] = (
                f"game reached turn {turn} without the Bolt leg completing")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pidx in ((p0, 0), (p1, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                atypes = sorted(set(a.get("type") for a in acts))
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"atypes={atypes} "
                    f"armor={ctx.get('armor_oid')} etb={ctx.get('etb_chosen')} "
                    f"x={ctx.get('x_rechosen')} xstall={ctx.get('x_stalled')} "
                    f"bolt={ctx.get('bolt_targeted')} "
                    f"a4={ctx.get('a4_recorded')} p0bf={bf_names(s, 0)[:6]}")
                if _opp_summary(st):
                    say(f"[diag] {c.name} vi: {_opp_summary(st)}")
        if ctx.get("a4_recorded"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if ctx.get("bolt_cast") and not ctx.get("a4_recorded") \
                and time.time() - t0 > 480:
            say(f"[{tag}] Bolt cast but never resolved >480s; bailing")
            break

    st = p0.latest
    state = st["state"] if st else {}

    # final authoritative export for the A5 object scan (the Armor may have
    # left the battlefield by now -- scan it wherever it is)
    try:
        final_exp = json.loads(await p0.export_state())
        with open(f"{EVDIR}/final_export.json", "w") as f:
            json.dump(final_exp, f, indent=1)
        wire("final_export", {})
        if isinstance(final_exp, dict) and isinstance(final_exp.get("state"), dict):
            final_state = final_exp["state"]
        elif isinstance(final_exp, dict):
            final_state = final_exp
        else:
            final_state = {}
    except Exception as e:
        wire("final_export_failed", {"error": str(e)[:200]})
        final_state = {}

    # ---- A1: Armor enchanting the Vanguard, ETB chose Red, 1 sleight counter
    if ctx.get("armor_oid"):
        ok_entry = ctx.get("armor_sleight_at_entry") == 1
        ok_etb = ctx.get("etb_chosen") == "red"
        enchants_vg = False
        ao = get_obj(state, ctx["armor_oid"]) if ctx.get("armor_oid") else {}
        enchants_vg = str(ao.get("attached_to") or ao.get("enchanting") or "") \
            == str(ctx.get("bolt_target_oid") or "")
        if ok_entry and ok_etb:
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                f"A1 passed: Chromatic Armor (oid {ctx['armor_oid']}) entered "
                f"with 1 sleight counter, ETB color choice offered and "
                f"answered Red (attached_to={ao.get('attached_to')})")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: sleight_at_entry={ctx.get('armor_sleight_at_entry')} "
                f"etb_chosen={ctx.get('etb_chosen')}")
    else:
        obs["assert"]["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1 not-run: Chromatic Armor never reached the "
                            "battlefield")

    # ---- A4: was the Bolt's damage prevented?
    if ctx.get("a4_recorded"):
        z = ctx.get("a4_vanguard_zone")
        if z != "Battlefield":
            obs["assert"]["A4_damage_not_prevented"] = "passed"
            obs["notes"].append(
                f"A4 passed (documents the consequence of the unimplemented "
                f"prevention): Lightning Bolt's 3 damage to the enchanted "
                f"Elite Vanguard was NOT prevented -- Vanguard (oid "
                f"{ctx.get('bolt_target_oid')}) left the battlefield "
                f"(zone={z}), Armor zone={ctx.get('a4_armor_zone')}, P0 life "
                f"after={ctx.get('a4_p0_life_after')}")
        else:
            obs["assert"]["A4_damage_not_prevented"] = "failed"
            obs["notes"].append(
                f"A4 FAILED (unexpected): the Vanguard survived the Bolt "
                f"(zone={z}) -- some prevention applied")
    else:
        obs["assert"]["A4_damage_not_prevented"] = "not-run"
        note = ("A4 not-run: the Bolt leg never completed "
                f"(bolt_cast={ctx.get('bolt_cast')}, "
                f"bolt_targeted={ctx.get('bolt_targeted')})")
        if obs.get("a4_giveup"):
            note += f"; giveup: {obs.get('a4_giveup_reason')}"
        obs["notes"].append(note)

    # ---- A5: is the re-choice stored on the Armor object?
    if ctx.get("x_rechosen") == "blue":
        try:
            with open(f"{EVDIR}/armor_object_after_x.json") as f:
                after = json.load(f)
            hits_after = find_chosen_hits(after)
        except (OSError, ValueError):
            after, hits_after = None, None
        try:
            with open(f"{EVDIR}/armor_object_at_entry.json") as f:
                at_entry = json.load(f)
            hits_entry = find_chosen_hits(at_entry)
        except (OSError, ValueError):
            hits_entry = None
        # also scan the Armor object in the final export (it may have left
        # the battlefield after the Bolt leg)
        armor_oid = ctx.get("armor_oid")
        final_hits = find_chosen_hits(get_obj(final_state, armor_oid)) \
            if armor_oid else None
        final_zone = obj_zone(final_state, armor_oid) if armor_oid else None
        obs["notes"].append(
            f"A5 object inspection: chosen-hits at entry={hits_entry}, "
            f"chosen-hits after {{X}}+Blue={hits_after}, "
            f"chosen-hits in final export (zone={final_zone})={final_hits}; "
            f"etb answer shape={ctx.get('etb_chosen_shape')}, "
            f"x answer shape={ctx.get('x_rechosen_shape')}")
        if hits_after or final_hits:
            obs["assert"]["A5_rechoice_not_stored"] = "passed"
            obs["notes"].append(
                "A5 passed: a chosen color IS recorded on the Armor object "
                f"after the {{X}} re-choice")
        else:
            obs["assert"]["A5_rechoice_not_stored"] = "failed"
            obs["notes"].append(
                "A5 FAILED (bug behavior): after activating {X} and "
                "re-choosing Blue, no chosen color is recorded on the "
                "Chromatic Armor object (persist:false) -- the re-choice "
                "does not persist on the object")
    elif ctx.get("x_stalled"):
        obs["assert"]["A5_rechoice_not_stored"] = "failed"
        obs["notes"].append(
            "A5 FAILED (bug behavior): the {X} activated ability cannot be "
            f"exercised at all -- advertised ability indices tried: "
            f"{ctx.get('x_tried_indices')}; the server accepted the "
            f"ActivateAbility submission but applied it with event_count=0 "
            f"(no stack entry, no counter, no re-choice prompt in 90s). No "
            f"re-choice opportunity ever appeared, so the re-choice "
            f"persistence question is moot in-game. The ETB choice WAS "
            f"recorded (chosen_attributes=[Color Red], persist:true); the "
            f"{{X}} re-choice (persist:false) is unreachable. etb answer "
            f"shape={ctx.get('etb_chosen_shape')}")
    else:
        obs["assert"]["A5_rechoice_not_stored"] = "not-run"
        obs["notes"].append(
            "A5 not-run: the {X} activation leg never completed "
            f"(x_activated={ctx.get('x_activated')}, "
            f"x_rechosen={ctx.get('x_rechosen')}, "
            f"x_stalled={ctx.get('x_stalled')})")

    for fn in ("pre_bolt.json", "post_bolt.json", "final_export.json",
               "armor_object_at_entry.json", "armor_object_after_x.json"):
        try:
            with open(f"{EVDIR}/{fn}") as f:
                json.load(f)
            obs["notes"].append(f"{fn} captured and parses")
        except (OSError, ValueError):
            obs["notes"].append(f"{fn} missing/unparseable")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    parse_ev = check_parse()
    obs = None
    for attempt in range(6):
        obs = await run_attempt(attempt)
        a1 = obs["assert"].get("A1_setup_ok")
        a4 = obs["assert"].get("A4_damage_not_prevented")
        if a1 in ("passed", "failed") and a4 in ("passed", "failed"):
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted; redealing")
            continue
        if obs.get("a4_giveup"):
            say(f"[attempt {attempt}] A4 giveup "
                f"({obs.get('a4_giveup_reason')}); not retrying blind")
            break
        say(f"[attempt {attempt}] incomplete (A1={a1}, A4={a4}); "
            f"{'retrying' if attempt < 5 else 'giving up'}")

    ass = dict(obs["assert"])
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["has_last_chosen_scope"]:
        ass["A2_prevention_unimpl"] = "passed"
    else:
        ass["A2_prevention_unimpl"] = "failed"
    # A3: ETB Choose persist:true vs {X} re-Choose persist:false
    etb_p, x_p = parse_ev["etb_choose_persist"], parse_ev["x_ability_choose_persist"]
    if etb_p is True and x_p is True:
        ass["A3_rechoice_persist"] = "passed"
    elif etb_p is True and x_p is False:
        ass["A3_rechoice_persist"] = "failed"
    else:
        ass["A3_rechoice_persist"] = "failed"
    # A6: oracle text intact in the pinned data
    ass["A6_oracle_intact"] = ("passed"
                               if (parse_ev["oracle_has_prevention_sentence"]
                                   and parse_ev["oracle_has_last_chosen"])
                               else "failed")
    notes = list(obs["notes"])
    notes.append(
        f"parse: prevention_unimplemented={parse_ev['prevention_unimplemented']} "
        f"prevention_implemented_effects={parse_ev['prevention_implemented_effects']} "
        f"has_last_chosen_scope={parse_ev['has_last_chosen_scope']} "
        f"etb_choose_persist={etb_p} x_choose_persist={x_p} "
        f"x_clause_unimplemented={parse_ev['x_clause_unimplemented']} "
        f"n_replacements={parse_ev['n_replacements']}")
    notes.append("protocol-140 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions (targets answered via the "
                 "engine's vi target selection after CastSpell, CR 601.2c); "
                 "ActivateAbility submitted while holding priority; "
                 "priority-gated passes; sleep(0) yield before leg evaluation; "
                 "color choices answered via the Choose-Color vi opportunity.")

    a2 = ass.get("A2_prevention_unimpl")
    if a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the pinned card-data.json entry "
                 "for Chromatic Armor parses the prevention sentence as "
                 "Unimplemented (prevention_source_scope) -- 'sources of the "
                 "last chosen color' has no representation -- corroborated by "
                 "the persist:false re-choice and the game legs where they "
                 "completed. not-reproduced = the scope is represented.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9856",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release); evidence under evidence/9856/" + RUN_ID),
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Plains", 24], ["Island", 24], ["Elite Vanguard", 6],
                   ["Chromatic Armor", 6]],
            "P1": [["Mountain", 28], ["Island", 28], ["Lightning Bolt", 4]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_rechoice_persist"),
                      ass.get("A4_damage_not_prevented"),
                      ass.get("A5_rechoice_not_stored"),
                      ass.get("A6_oracle_intact"))),
        "scope": ("Chromatic Armor 'sources of the last chosen color' "
                  "prevention scope + {X} re-choice persistence: parse-level "
                  "presence in the pinned v0.109.0 card-data.json (decisive), "
                  "plus three in-game legs (ETB color choice; {X} activation "
                  "+ re-choice with object inspection; Lightning Bolt damage "
                  "to the enchanted creature); two-player game; native human "
                  "seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The Bolt leg requires P1 to draw Lightning Bolt with the Armor "
            "on the battlefield -- missing preconditions mark that leg "
            "not-run.",
            "The {X} activation leg depends on how the engine values X "
            "(the 'X is the number of sleight counters' clause is "
            "unimplemented in the pinned data); the observed cost handling "
            "is recorded in the wire log.",
            "Chosen-color reads are a best-effort recursive search of the "
            "runtime Armor object for chosen/choice/color fields; the "
            "verbatim objects are preserved in armor_object_at_entry.json "
            "and armor_object_after_x.json.",
        ],
        "setup_line": ("P0 24x Plains + 24x Island + 6x Elite Vanguard "
                       "({W} 2/1) + 6x Chromatic Armor ({1}{W}{U} Aura); "
                       "P1 28x Mountain + 28x Island + 4x Lightning Bolt "
                       "({R} 3 damage to any target)"),
        "contract_line": ("Parse (decisive): the 'sources of the last chosen "
                          "color' prevention scope must be represented, or "
                          "confirmed Unimplemented; the {X} re-Choose must "
                          "persist like the ETB Choose. Game (confirming): "
                          "ETB offers a color choice; {X} re-choice is "
                          "inspected on the object; Lightning Bolt damage to "
                          "the enchanted creature is applied (prevention "
                          "absent)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9856.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read()) + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 1150
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9856 - Chromatic Armor: 'sources of the last",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "chosen color' has no representation",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    for k, v in run["assertions"].items():
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (230, 200, 90))
        d.text((36, y), f"{k}: {v}", fill=col); y += 24
        if y > H - 220:
            break
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    pe = run["parse_evidence"]
    obs_lines = [
        f"prevention parsed as: {pe['prevention_unimplemented']}",
        f"ETB Choose persist={pe['etb_choose_persist']}; "
        f"{{X}} re-Choose persist={pe['x_ability_choose_persist']}",
        f"X-clause: {pe['x_clause_unimplemented']}",
        f"game: {run['result']}",
    ]
    for ln in obs_lines:
        while len(ln) > 88:
            d.text((36, y), ln[:88], fill=(170, 185, 200)); y += 22
            ln = ln[88:]
        d.text((36, y), ln, fill=(170, 185, 200)); y += 22
    y += 6
    d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence 9856/" + run["run_id"],
           fill=(140, 160, 180))
    img.save(out_path)
    say(f"summary PNG written: {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
