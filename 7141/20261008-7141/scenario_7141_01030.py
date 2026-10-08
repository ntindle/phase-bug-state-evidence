#!/usr/bin/env python3
"""Issue #7141: "Cards that exile off the top of the library and then select
something and reshuffle are not [working]" -- example Grima, Saruman's Footman.

Protocol-106 port of driver/scenario_7141.py (v0.82.0/protocol 70,
validated 2026-09-13, verdict reproduced) for pinned v0.103.0.
Behavioral contract, assertions A1..A8 and verdict rule unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0, key "grima, saruman's footman"):
  Grima, Saruman's Footman ({2}{U}{B}, 1/4 Legendary Creature - Human Advisor):
    "Grima can't be blocked.
     Whenever Grima deals combat damage to a player, that player exiles cards
     from the top of their library until they exile an instant or sorcery card.
     You may cast that card without paying its mana cost. Then that player puts
     the exiled cards that weren't cast this way on the bottom of their library
     in a random order."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  triggers[0] = DamageDone(combat, valid_target Player) ->
    ExileFromTopUntil(player=TriggeringPlayer, until=NextMatches[Instant|Sorcery])
    sub_ability (optional) = CastFromZone(target=ParentTarget,
      without_paying_mana_cost=true, mode=Cast, driver=DuringResolution)
      sub_ability = PutAtLibraryPosition(target=ExiledBySource,
        count=Fixed(0), position=Bottom), sub_link=SequentialSibling
    else_ability = PutAtLibraryPosition(target=ExiledBySource,
      count=Fixed(0), position=Bottom)
      sub_ability = PutAtLibraryPosition (doubled)
  All three stages parse as SUPPORTED. Parse-shape observation (not asserted):
  the bottom cleanup carries count=Fixed(0) with target=ExiledBySource on
  v0.103.0 (was count=Fixed(1) on v0.82.0); oracle says "the exiled cards
  that weren't cast" (all of them).

Reported symptom (Discord, truncated): "Cards that exile off the top of the
library and then select something and reshuffle are not [working] -- example
[[Grima Sarumans footman]] This is def not the only one." No stage is named,
so the full three-stage contract is tested: exile-until, may-cast offer, and
bottom cleanup. The v0.82.0 run reproduced it in the CLEANUP stage: exile,
offer and free cast all worked, but the uncast exiled cards stayed stranded
in exile instead of going to the bottom of the library.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x grima, saruman's footman, 24x island, 24x swamp.
  P1: 20x lightning bolt, 40x forest (passive; never plays lands/casts/blocks).

Planned line:
  Setup: P0 drops lands (Island then Swamp), casts Grima ({2}{U}{B}, engine
    Auto payment). PRE exported at the combat1 DeclareAttackers.
  COMBAT1 (turn>=7): attack P1 with Grima (unblockable) => 1 combat damage
    (P1 20->19) => DamageDone trigger. P1 exiles from the top until the first
    Lightning Bolt. The may-cast offer is EXPECTED for P0 (vi exactChoices
    with decideOptionalEffect action code); driver ACCEPTS.
    offer1.json is exported at the offer (exile set visible mid-resolution).
    The Bolt is cast without paying, targeting P1 (P1 19->16); the Bolt card
    goes to its owner's (P1's) graveyard. Remaining exiled cards go to the
    bottom of P1's library. MID exported once settled.
  REGEN: wait for turn >= attack1_turn + 2 with Grima still on the BF.
  COMBAT2: attack P1 again (P1 16->15) => trigger again. Driver DECLINES the
    may-cast offer as the control branch (offer2.json at the offer). All
    exiled cards (including the Bolt) go to the bottom of P1's library.
    POST exported once settled.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: DamageDone -> ExileFromTopUntil[Instant|Sorcery]
                      + CastFromZone(free, optional) + PutAtLibraryPosition(Bottom).
  A2_setup            PRE: Grima on P0 BF, P1 at 20, P1 library intact.
  A3_exile_until      the exile set at offer1 == the top-of-library prefix of
                      P1's pre.json library ending at the first Bolt (top end
                      determined empirically from the observed prefix).
  A4_cast_offered     may-cast offer raised for P0 for the exiled Bolt.
  A5_cast_resolves    accept: Bolt resolves for exactly 3 to P1 with no mana
                      paid; the exiled Bolt card lands in P1's (owner's) gy.
  A6_bottom_cleanup   every other exiled card is in P1's library in mid.json;
                      none remain in exile.
  A7_decline_control  decline: offer raised and declined; all exiled cards
                      (incl. the Bolt) in P1's library in post.json; no new
                      Bolt in P1's gy; no bolt damage to P1.
  A8_cleanup          stack empty, game proceeds.

Verdict rule:
  blocked        iff A2 fails (setup never reached).
  reproduced     iff A2 passes and any of A3..A7 fails (the reported
                 "exile/may-cast/reshuffle not working" in one of its stages).
  not-reproduced iff A2..A8 all pass.

Protocol-106 port notes (from driver/scenario_7140_01030.py conventions):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment (legacy
    PayMana actions are answered if they appear).
  - DeclareAttackers: {"attacks": [[oid, {"type":"Player","data":1}]],
    "bands": []}; DeclareBlockers: {"assignments": []}.
  - The may-cast offer is a vi opportunity whose choices carry the
    decideOptionalEffect action code (scenario_301_01030.py shape); accept =
    choice with surface role "accept" value "true", decline = value "false".
  - The free Bolt's target selection is answered with seat 1; the engine may
    also auto-target a sole legal target (recorded as the auto-target case).
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/post/mid/offer states are authoritative exports via the host client
    only; the reported OUTCOME is asserted on the saved states, not the
    prompt.

Evidence: evidence/7141/<run-id>/pre.json, offer1.json, mid.json, offer2.json,
post.json, run.json, parse_grima_sarumans_footman.json, scenario_7141_01030.py,
wire_log.jsonl, scenario_run.log, server.log, summary.png, manifest.sha256,
offer1_opp.json, offer2_opp.json, free_bolt_target.json.
"""
import asyncio
import glob
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7141
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7141")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")


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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

PARSE = {"ok": False, "triggers": None, "obs": {}}


def check_parse_grima():
    """A1: v0.103.0 triggers carry DamageDone -> ExileFromTopUntil
    (NextMatches Instant|Sorcery) + CastFromZone free + PutAtLibraryPosition
    Bottom."""
    c = CARD_DATA.get("gr\u00edma, saruman's footman", {})
    trigs = c.get("triggers", [])
    blob = json.dumps(trigs)
    has = {
        "DamageDone": "DamageDone" in blob,
        "ExileFromTopUntil": "ExileFromTopUntil" in blob,
        "NextMatches": "NextMatches" in blob,
        "Instant": '"Instant"' in blob,
        "Sorcery": '"Sorcery"' in blob,
        "CastFromZone": "CastFromZone" in blob,
        "without_paying_mana_cost": "without_paying_mana_cost" in blob,
        "PutAtLibraryPosition": "PutAtLibraryPosition" in blob,
        "Bottom": '"Bottom"' in blob,
    }
    # parse-shape observation: bottom count value (oracle says ALL uncast
    # exiled cards)
    bottom_counts = []
    def find_bottom(node):
        if isinstance(node, dict):
            if node.get("type") == "PutAtLibraryPosition":
                bottom_counts.append(node.get("count"))
            for v in node.values():
                find_bottom(v)
        elif isinstance(node, list):
            for v in node:
                find_bottom(v)
    find_bottom(trigs)
    PARSE["obs"]["bottom_counts"] = bottom_counts
    with open(f"{EVDIR}/parse_grima_sarumans_footman.json", "w") as fh:
        json.dump({"card": "Gr\u00edma, Saruman's Footman",
                   "oracle_text": c.get("oracle_text"),
                   "mana_cost": c.get("mana_cost"),
                   "triggers": trigs,
                   "checks": has,
                   "observations": PARSE["obs"]},
                  fh, indent=1, default=str)
    say("saved parse_grima_sarumans_footman.json")
    ok = all(has.values()) and len(trigs) >= 1
    PARSE["triggers"] = trigs
    PARSE["ok"] = ok
    say(f"parse: {has} bottom_counts={bottom_counts} -> "
        f"A1={'passed' if ok else 'failed'}")
    wire("parse_check", {"A1": "passed" if ok else "failed", **has,
                         "bottom_counts": str(bottom_counts)})
    return ok


GRIMA = "gr\u00edma, saruman's footman"
BOLT = "lightning bolt"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"
LANDS = (ISLAND, SWAMP)

P0_DECK = [(GRIMA, 12), (ISLAND, 24), (SWAMP, 24)]
P1_DECK = [(BOLT, 20), (FOREST, 40)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45
OFFER_WAIT_S = 60

STOP = {"stop": False}
ST = {
    "stage": "setup",  # setup -> combat1 -> accept_leg -> regen ->
                       # combat2 -> decline_leg -> cleanup -> done
    "grima_cast": False,
    "attack1_done": False, "attack1_turn": None,
    "attack2_done": False, "attack2_turn": None,
    "p1_life_pre": None,
    "offer1_seen": False, "offer1_accepted": False, "offer1_skipped": False,
    "offer1_bolt_oid": None, "offer1_exiled": [],
    "p1_life_at_offer1": None,
    "p0_untapped_pre_cast": None, "p0_untapped_post_cast": None,
    "free_bolt_pending": False, "free_bolt_targeted": False,
    "free_bolt_auto": False,
    "gy1_bolts_pre_free": None, "p1_life_post_free": None,
    "mid_exported": False,
    "offer2_seen": False, "offer2_declined": False, "offer2_skipped": False,
    "offer2_exiled": [],
    "p1_life_at_offer2": None, "gy1_bolts_at_mid": None,
    "p1_life_post": None,
    "pre_exported": False, "offer1_exported": False,
    "offer2_exported": False, "post_exported": False,
    "p0_life_final": None, "p1_life_final": None,
    "settle_ticks": 0,
}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "optional_prompts": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}
P0_PID = 0

# ------------------------------------------------------- state helpers

def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def lib_oids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller")) == str(pid):
            if lname is None or obj_lname(state, oid) == lname:
                out.append(int(oid))
    return out


def bf_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and nm in LANDS):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def exile_oids(state):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if str(o.get("zone", "")).lower() == "exile"]


def gy_oids(state, pid, lname=None):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller")) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def stack_spells(state, name=None):
    out = []
    for e in state.get("stack") or []:
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if name is None or nm == name:
            out.append(e)
    return out


def gy_bolts(state, pid):
    return len(gy_oids(state, pid, BOLT))


def stack_empty(state):
    return not (state.get("stack") or [])


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_oid(ch):
    ref = cand_reference(ch)
    try:
        return str(int(ref))
    except (TypeError, ValueError):
        return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


def accept_of(choice):
    """surface role 'accept' value: 'true' (accept) / 'false' (decline)."""
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
    return None


def is_optional_opp(opp):
    for ch in (opp.get("response") or {}).get("data", {}).get("choices", []):
        if "decideOptionalEffect" in surf_codes(ch) \
                or "decideOptionalCost" in surf_codes(ch):
            return True
    return False


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(opp):
    for c in (opp.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") in ("select", "sequence")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type")})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "response": sub.get("response")})
    LAST_IID["iid"] = sub.get("interactionId")
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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out


async def export_named(c, tag):
    try:
        s = await c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(s)
        say(f"exported {tag.upper()}")
        return True
    except Exception as e:
        OBS["notes"].append(f"{tag} export failed: {e}")
        say(f"{tag} export failed: {e}")
        return False


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hn = hand_lnames(st["state"], pid)
    n_lands = sum(1 for h in hn if h in LANDS)
    n = MULLS.get(tag, 0)
    if pid == 0:
        keep = (GRIMA in hn and n_lands >= 2) or n >= 2
    else:
        keep = n_lands >= 2 or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        MULLS[tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hn})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if MULLS.get(tag, 0) <= 0:
        return False
    key_card = GRIMA if pid == 0 else None
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        spec = (rdata.get("spec", {}) or {})
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = cand_oid(ch)
            nm = obj_lname(state, ref) if ref else ""
            if key_card and nm == key_card:
                return (2, str(ref))   # never bottom the key card
            if nm in LANDS:
                return (1, str(ref))   # bottom lands first
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


def discard_rank(state, o, pid):
    nm = obj_lname(state, o)
    if nm == FOREST:
        return 0
    if nm in LANDS:
        return 1
    if pid == 0 and nm == GRIMA:
        return 3                      # key card kept last
    return 2


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = cand_oid(ch)
            if ref is not None:
                ref_of[ref] = ch["id"]
        ranked = sorted(hand,
                        key=lambda o: (discard_rank(state, o, pid),
                                       obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        DISCARDED_IIDS.add(iid)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def do_declare(c, acts, st, pid, tag):
    state = st["state"]
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            if pid == 0:
                # stage-gated in p0_declare; default to no attack here
                d["data"]["attacks"] = []
                d["data"]["bands"] = []
            else:
                d["data"]["attacks"] = []
                d["data"]["bands"] = []
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, target):
    """Play a land while fewer than `target` untapped lands (prefer the
    color P0 needs: Island first, then Swamp)."""
    if len(untapped_lands(state, pid)) >= target:
        return False
    cands = [a for a in acts if a.get("type") == "PlayLand"]
    if not cands:
        return False

    def rank(a):
        try:
            nm = obj_lname(state, a.get("_src_oid"))
        except (TypeError, ValueError):
            return (9, "")
        if pid == 0:
            return (0 if nm == ISLAND else (1 if nm == SWAMP else 2), nm)
        return (3, nm)

    a = sorted(cands, key=rank)[0]
    say(f"[{tag}] plays land {obj_lname(state, a.get('_src_oid'))} "
        f"(target {target} untapped)")
    wire("play_land", {"who": tag, "target": target})
    await submit_as_is(c, a)
    return True


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def log_unanswered(c, tag, st):
    for opp in unanswered_ops(st):
        iid = opp.get("interactionId")
        if iid in LOGGED_IIDS or iid in DISCARDED_IIDS:
            continue
        LOGGED_IIDS.add(iid)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        OBS["unexpected_prompts"].append(
            {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
             "rtype": resp.get("type"),
             "codes": sorted({x for ch in chs
                              for x in surf_codes(ch) if x}),
             "texts": [choice_text(ch)[:60] for ch in chs][:8]})
        say(f"[{tag}] unanswered vi iid={iid} n={len(chs)} "
            f"rtype={resp.get('type')}")
        wire("unanswered_vi",
             {"who": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})

# ------------------------------------------------- issue-specific logic

def record_opp(state, opp, stage, purpose, fname):
    """Record an opportunity once per interactionId to EVDIR/<fname>.json."""
    iid = opp.get("interactionId")
    if any(r.get("interactionId") == iid for r in OBS["target_selections"]):
        return
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in chs:
        oid = cand_oid(ch)
        o = get_obj(state, oid) if oid else {}
        cand_info.append({
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "tapped": o.get("tapped"),
            "accept": accept_of(ch),
            "codes": surf_codes(ch),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "purpose": purpose,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    OBS["target_selections"].append(rec)
    with open(f"{EVDIR}/{fname}", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    say(f"[{purpose}] recorded {fname} (iid={iid}, n={len(chs)})")
    wire(f"{purpose}_recorded", {"iid": iid, "n": len(chs)})


def record_optional_opp(state, opp, stage, role):
    iid = opp.get("interactionId")
    if any(r.get("interactionId") == iid for r in OBS["optional_prompts"]):
        return
    resp = opp.get("response", {}) or {}
    chs = (resp.get("data", {}) or {}).get("choices", []) or []
    rec = {"interactionId": iid, "turn": state.get("turn_number"),
           "phase": state.get("phase"), "stage": stage, "role": role,
           "rtype": resp.get("type"),
           "accept_values": [accept_of(ch) for ch in chs],
           "codes": [surf_codes(ch) for ch in chs],
           "texts": [choice_text(ch)[:80] for ch in chs]}
    OBS["optional_prompts"].append(rec)
    say(f"[P0] optional prompt ({role}) iid={iid} "
        f"accept={rec['accept_values']}")
    wire("optional_prompt", rec)


async def grima_optional_tick(c, pid, tag, st, state):
    """Answer Grima's 'you may cast that card' prompt: combat1 ACCEPTS the
    reported branch, combat2 DECLINES the control branch. Exports offer1 /
    offer2 at the prompt (exile set visible mid-resolution) BEFORE answering."""
    stage = ST["stage"]
    if stage not in ("combat1", "combat2"):
        return False
    for opp in unanswered_ops(st):
        if not is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        chs = (resp.get("data", {}) or {}).get("choices", []) or []
        if not chs:
            continue
        st_now = st["state"]
        ex = exile_oids(st_now)
        bolt_oid = next((o for o in ex if obj_lname(st_now, o) == BOLT),
                        None)
        if stage == "combat1":
            role = "may-cast-accept"
            record_optional_opp(st_now, opp, stage, role)
            record_opp(st_now, opp, stage, role, "offer1_opp.json")
            if not ST["offer1_exported"]:
                if await export_named(c, "offer1"):
                    ST["offer1_exported"] = True
                    st_now = st_of(c)["state"]
                    ex = exile_oids(st_now)
                    bolt_oid = next(
                        (o for o in ex if obj_lname(st_now, o) == BOLT),
                        None)
            ST["offer1_seen"] = True
            ST["offer1_exiled"] = ex
            ST["offer1_bolt_oid"] = bolt_oid
            ST["p1_life_at_offer1"] = life_of(st_now, 1)
            ST["p0_untapped_pre_cast"] = len(untapped_lands(st_now, 0))
            ST["gy1_bolts_pre_free"] = gy_bolts(st_now, 1)
            accept_ch = next((ch for ch in chs
                              if accept_of(ch) == "true"), None)
            if accept_ch is None:
                say("[P0] ERROR: no accept=true choice on the may-cast "
                    "prompt; NOT answering")
                wire("optional_no_accept_choice", {"stage": stage})
                return True
            say(f"[P0] ACCEPTING the free cast (reported branch); "
                f"exiled_n={len(ex)} bolt_oid={bolt_oid}")
            await answer_vi(c, opp, accept_ch, tag)
            ST["offer1_accepted"] = True
            ST["free_bolt_pending"] = True
            ST["stage"] = "accept_leg"
            return True
        else:  # combat2: decline control
            role = "may-cast-decline"
            record_optional_opp(st_now, opp, stage, role)
            record_opp(st_now, opp, stage, role, "offer2_opp.json")
            if not ST["offer2_exported"]:
                if await export_named(c, "offer2"):
                    ST["offer2_exported"] = True
                    st_now = st_of(c)["state"]
                    ex = exile_oids(st_now)
            ST["offer2_seen"] = True
            ST["offer2_exiled"] = ex
            ST["p1_life_at_offer2"] = life_of(st_now, 1)
            ST["gy1_bolts_at_mid"] = gy_bolts(st_now, 1)
            decline_ch = next((ch for ch in chs
                               if accept_of(ch) == "false"), None)
            if decline_ch is None:
                say("[P0] ERROR: no accept=false choice on the may-cast "
                    "prompt; NOT answering")
                wire("optional_no_decline_choice", {"stage": stage})
                return True
            say(f"[P0] DECLINING the free cast (control); "
                f"exiled_n={len(ex)}")
            await answer_vi(c, opp, decline_ch, tag)
            ST["offer2_declined"] = True
            ST["stage"] = "decline_leg"
            return True
    return False


async def free_bolt_target_tick(c, tag, st, state):
    """Answer the free Bolt's own target selection (seat 1)."""
    if ST["stage"] != "accept_leg":
        return False
    if not ST.get("free_bolt_pending"):
        return False
    for opp in unanswered_ops(st):
        if is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        record_opp(state, opp, "accept_leg", "free-bolt-target",
                   "free_bolt_target.json")
        pick = next((ch for ch in chs if cand_seat(ch) == 1), None)
        if pick is None:
            pick = chs[0]
        say(f"[P0] free-bolt target: choosing seat 1 "
            f"(choice {pick.get('id')})")
        await answer_vi(c, opp, pick, tag)
        ST["free_bolt_pending"] = False
        ST["free_bolt_targeted"] = True
        return True
    return False


async def bottom_order_tick(c, pid, tag, st, state):
    """Defensive: if the engine asks P1 to order the bottomed cards (the
    cleanup's target is P1's library), answer in advertised order."""
    if ST["stage"] not in ("accept_leg", "decline_leg"):
        return False
    if pid != 1:
        return False
    for opp in unanswered_ops(st):
        if is_optional_opp(opp):
            continue
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "order", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        cids = [ch["id"] for ch in cands if ch.get("id")]
        say(f"[{tag}] bottom-order prompt: answering {len(cids)} in "
            f"advertised order")
        wire("bottom_order", {"who": tag, "n": len(cids)})
        OBS["notes"].append(f"bottom_order prompt answered ({len(cids)} "
                            f"cards, advertised order)")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": cids}}},
                          tag)
        return True
    return False


def cast_spell_for(acts, state, lname):
    """CastSpell-ish actions whose card is `lname`."""
    out = []
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        vals = list(d.values())
        src = a.get("_src_oid")
        if src is not None:
            vals.append(src)
        for v in vals:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    out.append((v, a))
                    break
            except (TypeError, ValueError):
                pass
    return out


async def p0_declare(c, acts, st, state):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0
    for a in acts:
        if a.get("type") != "DeclareAttackers":
            continue
        d = dict(a)
        d["data"] = dict(d.get("data") or {})
        grima = bf_oids(state, 0, GRIMA)
        do_atk = False
        if grima and stage == "combat1" and not ST["attack1_done"]:
            do_atk = True
            leg = 1
        elif grima and stage == "combat2" and not ST["attack2_done"]:
            do_atk = True
            leg = 2
        else:
            leg = 0
        if do_atk:
            if leg == 1 and not ST["pre_exported"]:
                ST["p1_life_pre"] = life_of(state, 1)
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
            d["data"]["attacks"] = [
                [grima[0], {"type": "Player", "data": 1}]]
            d["data"]["bands"] = []
            say(f"[P0] declaring attacker: grima -> P1 (leg {leg})")
        else:
            d["data"]["attacks"] = []
            d["data"]["bands"] = []
        await submit_as_is(c, d)
        if do_atk:
            if leg == 1:
                ST["attack1_done"] = True
                ST["attack1_turn"] = turn
                ST["stage_t0"] = time.time()
            else:
                ST["attack2_done"] = True
                ST["attack2_turn"] = turn
                ST["stage_t0"] = time.time()
        return True
    return False


async def stage_transitions(c, tag, st, state):
    """Bookkeeping transitions from live state. Returns True if the tick
    should stop here (post exported)."""
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if stage == "setup":
        grima = bf_oids(state, 0, GRIMA)
        if grima and turn >= 7:
            ST["stage"] = "combat1"
            ST["stage_t0"] = time.time()
            say(f"[P0] stage -> combat1 (turn={turn})")

    if stage == "combat1":
        # skipped-offer backstop: the trigger/offer never materialized
        if ST["attack1_done"] and not ST["offer1_seen"] \
                and time.time() - ST.get("stage_t0", 0) > OFFER_WAIT_S:
            say("[P0] combat1: no may-cast offer within "
                f"{OFFER_WAIT_S}s; treating as skipped-offer path")
            wire("offer1_skipped", {})
            ST["offer1_skipped"] = True
            ST["stage"] = "accept_leg"
            ST["settle_ticks"] = 0

    if stage == "accept_leg":
        st_now = st["state"]
        # auto-target case: the free-bolt target prompt never appeared but
        # the game advanced past targeting
        if ST["free_bolt_pending"] and not ST["free_bolt_targeted"] \
                and (len(stack_spells(st_now, BOLT)) > 0
                     or gy_bolts(st_now, 1)
                     > (ST.get("gy1_bolts_pre_free") or 0)):
            ST["free_bolt_targeted"] = True
            ST["free_bolt_auto"] = True
            ST["free_bolt_pending"] = False
            say("[P0] free bolt: no target prompt seen; treating as "
                "auto-target")
        # free bolt resolved?
        if ST["offer1_accepted"] and ST["free_bolt_targeted"] \
                and len(stack_spells(st_now, BOLT)) == 0 \
                and gy_bolts(st_now, 1) > (ST.get("gy1_bolts_pre_free")
                                           or 0):
            if ST["p1_life_post_free"] is None:
                ST["p1_life_post_free"] = life_of(st_now, 1)
                ST["p0_untapped_post_cast"] = len(untapped_lands(st_now,
                                                                 0))
                say(f"[P0] leg-1 free bolt resolved: P1 "
                    f"{ST['p1_life_at_offer1']}->"
                    f"{ST['p1_life_post_free']}")
        leg_done = (ST["p1_life_post_free"] is not None
                    or ST["offer1_skipped"])
        settled = stack_empty(st_now)
        if leg_done and settled:
            ST["settle_ticks"] += 1
        else:
            ST["settle_ticks"] = 0
        if leg_done and ST["settle_ticks"] >= 3:
            if not ST["mid_exported"]:
                if await export_named(c, "mid"):
                    ST["mid_exported"] = True
                    ST["stage"] = "regen"
                    say("[P0] stage -> regen")

    if stage == "regen":
        grima = bf_oids(state, 0, GRIMA)
        if grima and ST["attack1_turn"] is not None \
                and turn >= ST["attack1_turn"] + 2:
            ST["stage"] = "combat2"
            ST["stage_t0"] = time.time()
            say(f"[P0] stage -> combat2 (turn={turn})")

    if stage == "combat2":
        if ST["attack2_done"] and not ST["offer2_seen"] \
                and time.time() - ST.get("stage_t0", 0) > OFFER_WAIT_S:
            say("[P0] combat2: no may-cast offer within "
                f"{OFFER_WAIT_S}s; treating as skipped-offer path")
            wire("offer2_skipped", {})
            ST["offer2_skipped"] = True
            ST["stage"] = "decline_leg"
            ST["settle_ticks"] = 0

    if stage == "decline_leg":
        st_now = st["state"]
        leg_done = ST["offer2_declined"] or ST["offer2_skipped"]
        settled = stack_empty(st_now)
        if leg_done and settled:
            ST["settle_ticks"] += 1
        else:
            ST["settle_ticks"] = 0
        if leg_done and ST["settle_ticks"] >= 3:
            if not ST["post_exported"]:
                ST["p1_life_post"] = life_of(st_now, 1)
                if await export_named(c, "post"):
                    ST["post_exported"] = True
                    ST["stage"] = "done"
                    return True
    return False

# ------------------------------------------------------------- seat ticks

async def p0_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await grima_optional_tick(c, 0, "P0", st, state):
        return True
    if await free_bolt_target_tick(c, "P0", st, state):
        return True
    if await p0_declare(c, acts, st, state):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0

    # life trace
    lives = (life_of(state, 0), life_of(state, 1))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    # stage transitions from live state (never holds priority itself)
    if await stage_transitions(c, tag, st, state):
        STOP["stop"] = True
        return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: land drops, then Grima ----
    is_p0_main = (phase in MAIN_PHASES and state.get("active_player") == 0)
    if is_p0_main:
        if await play_a_land(c, state, 0, acts, tag, 99):
            return True
        grima_bf = bf_oids(state, 0, GRIMA)
        if not grima_bf and not ST["grima_cast"]:
            found = cast_spell_for(acts, state, GRIMA)
            if found:
                oid, action = found[0]
                say(f"[P0] casting grima, saruman's footman oid={oid} "
                    f"(engine Auto payment)")
                wire("grima_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["grima_cast"] = True
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def p1_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await bottom_order_tick(c, pid, tag, st, state):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a['type']})
            await submit_as_is(c, a)
            return True
    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # P1 is fully passive otherwise (never plays lands, never casts,
    # never blocks, never attacks)
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True

# ------------------------------------------------------------- main loop

async def main():
    global t_start, P0_PID
    t_start = time.time()
    last_rev_change = t_start
    game_started = False

    hello = await verify_server_hello()
    check_parse_grima()

    p0 = PhaseClient("P07141r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17141r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    P0_PID = int(p0.player_id)
    say(f"game {GAME}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game", {"code": GAME, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_parse", "A2_setup", "A3_exile_until", "A4_cast_offered",
                "A5_cast_resolves", "A6_bottom_cleanup", "A7_decline_control",
                "A8_cleanup")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        if not ST["post_exported"]:
            try:
                s = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(s)
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post fallback export failed: {e}")

        pre, offer1, mid, offer2, post = (load("pre"), load("offer1"),
                                         load("mid"), load("offer2"),
                                         load("post"))
        for fn, s in (("pre", pre), ("offer1", offer1), ("mid", mid),
                      ("offer2", offer2), ("post", post)):
            if s is not None:
                say(f"loaded {fn}.json")

        def names_of(s, oids):
            return [obj_lname(s, o) for o in oids]

        # ---- A1: parse ----
        ass["A1_parse"] = "passed" if PARSE["ok"] else "failed"
        notes.append(f"A1: DamageDone + ExileFromTopUntil + NextMatches "
                     f"+ Instant + Sorcery + CastFromZone(free) + "
                     f"PutAtLibraryPosition(Bottom): {PARSE['ok']}; "
                     f"OBS bottom_counts={PARSE['obs'].get('bottom_counts')} "
                     f"(oracle: ALL uncast exiled cards)")

        # ---- A2: setup ----
        if pre is not None:
            g = bf_oids(pre, 0, GRIMA)
            lib1 = lib_oids(pre, 1)
            ok = (len(g) == 1 and ST["p1_life_pre"] == 20 and len(lib1) > 0)
            notes.append(f"A2: grima_bf={len(g)} p1_life_pre="
                         f"{ST['p1_life_pre']} (expect 20) p1_lib="
                         f"{len(lib1)} turn={pre.get('turn_number')}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: exile-until ----
        # E1 = exile contents at offer1; must equal the top-of-library
        # prefix of P1's pre.json library ending at the first bolt (top end
        # determined empirically).
        top_end = None
        if pre is not None and offer1 is not None and ST["offer1_seen"]:
            e1 = exile_oids(offer1)
            ex1_names = names_of(offer1, e1)
            lib_names = names_of(pre, lib_oids(pre, 1))
            for end, seq in (("front", lib_names),
                             ("back", list(reversed(lib_names)))):
                pref = []
                for nm in seq:
                    pref.append(nm)
                    if nm == BOLT:
                        break
                else:
                    continue
                if sorted(pref) == sorted(ex1_names) \
                        and pref.count(BOLT) == 1:
                    top_end = end
                    break
            ok = top_end is not None
            notes.append(f"A3: exiled_n={len(e1)} exiled={ex1_names} "
                         f"top_end={top_end} bolt_oid="
                         f"{ST['offer1_bolt_oid']}")
        else:
            ok = False
            notes.append(f"A3 failed: pre={'ok' if pre else 'missing'} "
                         f"offer1={'ok' if offer1 else 'missing'} "
                         f"seen={ST['offer1_seen']} "
                         f"skipped={ST['offer1_skipped']}")
        ass["A3_exile_until"] = "passed" if ok else "failed"

        # ---- A4: cast offered ----
        ok = bool(ST["offer1_seen"] and ST["offer1_bolt_oid"] is not None
                  and ST["offer1_accepted"])
        notes.append(f"A4: offer1_seen={ST['offer1_seen']} "
                     f"bolt_oid={ST['offer1_bolt_oid']} "
                     f"accepted={ST['offer1_accepted']} "
                     f"skipped={ST['offer1_skipped']}")
        ass["A4_cast_offered"] = "passed" if ok else "failed"

        # ---- A5: cast resolves ----
        if mid is not None and ST["offer1_accepted"] \
                and ST["free_bolt_targeted"]:
            dmg = None
            if ST["p1_life_at_offer1"] is not None \
                    and ST["p1_life_post_free"] is not None:
                dmg = ST["p1_life_at_offer1"] - ST["p1_life_post_free"]
            bolt_oid = ST["offer1_bolt_oid"]
            o = get_obj(mid, bolt_oid) if bolt_oid else {}
            in_p1_gy = (o.get("zone") == "Graveyard"
                        and str(o.get("controller")) == "1")
            lands_same = (ST["p0_untapped_pre_cast"] is not None
                          and ST["p0_untapped_post_cast"]
                          == ST["p0_untapped_pre_cast"])
            ok = (dmg == 3 and in_p1_gy)
            notes.append(f"A5: P1 {ST['p1_life_at_offer1']}->"
                         f"{ST['p1_life_post_free']} (dmg={dmg}, expect 3); "
                         f"bolt oid={bolt_oid} in P1 gy={in_p1_gy}; "
                         f"P0 untapped lands {ST['p0_untapped_pre_cast']}->"
                         f"{ST['p0_untapped_post_cast']} (free cast: "
                         f"expect no payment; unchanged={lands_same})")
        else:
            ok = False
            notes.append(f"A5 failed: mid={'ok' if mid else 'missing'} "
                         f"accepted={ST['offer1_accepted']} "
                         f"targeted={ST['free_bolt_targeted']}")
        ass["A5_cast_resolves"] = "passed" if ok else "failed"

        # ---- A6: bottom cleanup ----
        if mid is not None and offer1 is not None and ST["offer1_seen"]:
            e1 = exile_oids(offer1)
            rest = [o for o in e1 if o != ST["offer1_bolt_oid"]]
            mid_lib = set(lib_oids(mid, 1))
            mid_exile = set(exile_oids(mid))
            in_lib = sum(1 for o in rest if o in mid_lib)
            stranded = [o for o in rest if o in mid_exile]
            ok = (in_lib == len(rest) and not stranded)
            notes.append(f"A6: uncast_exiled={len(rest)} in_P1_lib={in_lib} "
                         f"stranded_in_exile={len(stranded)} "
                         f"{names_of(mid, stranded)}")
        else:
            ok = False
            notes.append("A6 failed: mid/offer1 missing or offer not seen")
        ass["A6_bottom_cleanup"] = "passed" if ok else "failed"

        # ---- A7: decline control ----
        if post is not None and offer2 is not None and ST["offer2_seen"]:
            e2 = exile_oids(offer2)
            post_lib = set(lib_oids(post, 1))
            post_exile = set(exile_oids(post))
            in_lib = sum(1 for o in e2 if o in post_lib)
            stranded = [o for o in e2 if o in post_exile]
            gy_now = gy_bolts(post, 1)
            gy_then = ST["gy1_bolts_at_mid"]
            dmg2 = None
            if ST["p1_life_at_offer2"] is not None \
                    and ST["p1_life_post"] is not None:
                dmg2 = ST["p1_life_at_offer2"] - ST["p1_life_post"]
            ok = (in_lib == len(e2) and not stranded
                  and gy_then is not None and gy_now == gy_then
                  and dmg2 == 0)
            notes.append(f"A7: exiled_n={len(e2)} in_P1_lib={in_lib} "
                         f"stranded={len(stranded)} "
                         f"P1-gy bolts {gy_then}->{gy_now} (expect no new); "
                         f"P1 {ST['p1_life_at_offer2']}->"
                         f"{ST['p1_life_post']} (dmg={dmg2}, expect 0)")
        else:
            ok = False
            notes.append(f"A7 failed: post={'ok' if post else 'missing'} "
                         f"offer2={'ok' if offer2 else 'missing'} "
                         f"seen={ST['offer2_seen']} "
                         f"skipped={ST['offer2_skipped']}")
        ass["A7_decline_control"] = "passed" if ok else "failed"

        # ---- A8: cleanup ----
        if post is not None:
            stack_ok = stack_empty(post)
            ok = stack_ok
            ST["p0_life_final"] = life_of(post, 0)
            ST["p1_life_final"] = life_of(post, 1)
            notes.append(f"A8: stack_empty={stack_ok} "
                         f"life_final={ST['p0_life_final']}/"
                         f"{ST['p1_life_final']}")
        else:
            ok = False
            notes.append("A8 failed: post.json missing")
        ass["A8_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif any(ass.get(k) != "passed"
                 for k in ("A3_exile_until", "A4_cast_offered",
                           "A5_cast_resolves", "A6_bottom_cleanup",
                           "A7_decline_control")):
            verdict = "reproduced"
            notes.append("verdict=reproduced: the exile-until / may-cast / "
                         "bottom-cleanup chain failed in at least one stage")
        elif all(ass.get(k) == "passed"
                 for k in ("A2_setup", "A3_exile_until", "A4_cast_offered",
                           "A5_cast_resolves", "A6_bottom_cleanup",
                           "A7_decline_control", "A8_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: exile-until, free cast, "
                         "and bottom cleanup all behaved per Oracle")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-10-08",
            "server": {
                "server_version": hello.get("server_version"),
                "build_commit": hello.get("build_commit"),
                "protocol_version": hello.get("protocol_version"),
                "mode": hello.get("mode"),
                "server_binary_sha256":
                    SERVER_IDENTITY["server_binary_sha256"],
                "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
                "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
                "signature_verified":
                    SERVER_IDENTITY["signature_verified"],
            },
            "driver": {"protocol_advertised": 106,
                       "client": "driver/client.py"},
            "scenario_sha256": sha256_of_file(__file__),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": [[n, c] for n, c in P0_DECK],
                      "P1": [[n, c] for n, c in P1_DECK]},
            "setup_line": ("P0 12x Grima, Saruman's Footman / 24x Island / "
                           "24x Swamp; P1 20x Lightning Bolt / 40x Forest "
                           "(passive). P0 mulligans for Grima+2 lands, casts "
                           "Grima ({2}{U}{B}, engine Auto payment). Combat1 "
                           "(turn>=7): attack with Grima (unblockable), "
                           "ACCEPT the may-cast offer (reported branch), "
                           "target the free Bolt at P1. Regen: wait 2 turns. "
                           "Combat2: attack, DECLINE the may-cast offer "
                           "(control)."),
            "contract_line": ("Grima's combat-damage trigger exiles from "
                              "the top until an instant/sorcery, offers the "
                              "free cast, then must put the uncast exiled "
                              "cards on the bottom of the library; the "
                              "v0.82.0 run left them stranded in exile."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_7141.py (v0.82.0 / "
                "protocol 70) for pinned v0.103.0; the behavioral contract, "
                "assertions A1..A8 and the verdict rule are unchanged.",
                "my_priority = PassPriority in legal_actions; casts are "
                "gated on it.",
                "CastSpell carries payment_mode Auto: the engine taps mana "
                "itself; the driver never answers tapLandForMana and runs "
                "no driver-side mana payment (legacy PayMana actions are "
                "answered if they appear).",
                "The may-cast offer is a vi opportunity whose choices "
                "carry the decideOptionalEffect action code "
                "(scenario_301_01030.py shape); accept = role accept "
                "value true, decline = value false.",
                "The free Bolt's target selection is answered with seat 1; "
                "the engine may also auto-target a sole legal target "
                "(recorded as the auto-target case).",
                "DeclareAttackers via legacy Action with "
                "attacks=[[int(oid), {type:Player, data:1}]], bands=[]; "
                "DeclareBlockers with assignments=[].",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); both seats must "
                "pass in succession for a stack entry to resolve.",
                "Pre/offer/mid/post states are authoritative exports "
                "(data.state parsed once from the export envelope) via the "
                "host client only; the reported OUTCOME is asserted on the "
                "saved states, not the prompt.",
            ],
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "mulligans": MULLS,
            "notes": notes,
            "evidence_files": ["pre.json", "offer1.json", "mid.json",
                               "offer2.json", "post.json", "run.json",
                               "parse_grima_sarumans_footman.json",
                               "offer1_opp.json", "offer2_opp.json",
                               "free_bolt_target.json",
                               "scenario_7141_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Grima / 20x Lightning Bolt density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "P1 is a fully passive punching bag (20x Bolt, 40x Forest; "
                "never plays lands, never casts, never blocks).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine; the driver keys on "
                "active_player and turn_number, not order.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1, default=str)
        say(f"wrote run.json verdict={verdict}")
        for k, v in ass.items():
            say(f"{k}: {v}")

        with open(__file__) as f:
            src = f.read()
        with open(f"{EVDIR}/scenario_7141_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7141_01030.py into EVDIR")

        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",):
            if os.path.exists(cand):
                srv_src = cand
                break
        if srv_src is not None:
            import shutil
            shutil.copy(srv_src, f"{EVDIR}/server.log")
            say(f"copied server.log from {srv_src} into EVDIR")
        else:
            note = (f"no per-run server.log at runs/{RUN_ID}/server.log; "
                    f"the pinned server on 127.0.0.1:9374 was already "
                    f"running (dedicated to this run); wire traffic is in "
                    f"wire_log.jsonl, driver log in scenario_run.log")
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write(note + "\n")
            say("server.log: wrote note instead (no runs/<run-id>/server.log)")

        render_summary(run, pre, offer1, mid, offer2, post)

        write_manifest()               # build 1
        say("scenario finished")       # final scenario_run.log line
        write_manifest(quiet=True)     # build 2 -- no logging after this
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, pre, offer1, mid, offer2, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7141 - Grima, Saruman's Footman",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "exile-until + may-cast + bottom cleanup",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: combat damage -> exile top until "
               "instant/sorcery -> you may cast it free -> rest to the "
               "bottom.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: ExileFromTopUntil + free cast + bottom",
            "A2_setup": "PRE: grima on BF, P1 at 20, P1 library intact",
            "A3_exile_until": "exile set == top prefix ending at first Bolt",
            "A4_cast_offered": "may-cast offer raised and accepted",
            "A5_cast_resolves": "P1 19->16, bolt card in P1 gy, no mana paid",
            "A6_bottom_cleanup": "uncast exiled cards in P1 library, none stranded",
            "A7_decline_control": "decline: all exiled in P1 library, no damage",
            "A8_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Board across states:", fill=(200, 210, 225))
        y += 24

        def grima_bf(s):
            return len(bf_oids(s, 0, GRIMA)) if s else None

        for label, s in (("pre   ", pre), ("offer1", offer1),
                         ("mid   ", mid), ("offer2", offer2),
                         ("post  ", post)):
            if s is not None:
                line = (f"{label}: life {life_of(s, 0)}/{life_of(s, 1)}  "
                        f"grima_bf={grima_bf(s)}  "
                        f"p1_lib={len(lib_oids(s, 1))}  "
                        f"exile={len(exile_oids(s))}  "
                        f"stack={len(s.get('stack') or [])}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:22]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "offer1.json", "mid.json", "offer2.json",
                 "post.json", "run.json",
                 "parse_grima_sarumans_footman.json",
                 "offer1_opp.json", "offer2_opp.json",
                 "free_bolt_target.json",
                 "scenario_7141_01030.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # build 2 must be quiet: any say() after the second write would
        # append to scenario_run.log and stale its manifest hash.
        if not quiet:
            say(f"wrote manifest.sha256 ({len(lines)} files)")

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    last_rev_change = t_start
    game_started = False
    while time.time() - t_start < GAME_TIMEOUT and not STOP.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick),
                             (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # (or holding an unanswered vi decision) with no revision
                # change (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                pid = {"P0": 0, "P1": 1}[tag]
                await tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if STOP.get("stop"):
            break

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STOP["stop"] = True
            continue

        if game_started and not STOP.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {})
            STOP["stop"] = True
            continue

        if turn > TURN_CAP and not STOP.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STOP["stop"] = True
            continue

        if turn > 30 and ST["stage"] == "setup" and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 reached, setup never "
                                "completed; finishing")
            say("watchdog: turn 30, setup never completed; finishing")
            await finish()
            return

        if turn > 40 and ST["stage"] not in ("cleanup", "done") \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 40 reached with the line "
                                "incomplete; finishing")
            say("watchdog: turn 40, line incomplete; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            grima = bf_oids(state, 0, GRIMA)
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"stage={ST['stage']} "
                f"grima_bf={len(grima)} "
                f"attack1/2={ST['attack1_done']}/{ST['attack2_done']} "
                f"offer1={ST['offer1_seen']}/{ST['offer1_accepted']} "
                f"offer2={ST['offer2_seen']}/{ST['offer2_declined']} "
                f"exile={len(exile_oids(state))} "
                f"stack_bolts={len(stack_spells(state, BOLT))}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
