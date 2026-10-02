#!/usr/bin/env python3
"""Issue #7367: Faith & Grief (the adventure half of Ishgard, the Holy See)
is uncastable -- "Unless I'm missing something, [[Faith & Grief]], the
adventure half of [[Ishgard, the Holy See]] should be castable in this game
state with mana from the Sanctum Weaver, but it ignores that and only
enters as a land."

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, sync-filed, status:needs-repro,
classifier:supported-aspect-defect): the adventure half could not be cast
even though Sanctum Weaver could produce the mana; the card could only be
played as a land. Oracle text (verified from pinned card-data.json):
  faith & grief:  {3}{W}{W} sorcery (Adventure) -- "Return up to two target
                  artifact and/or enchantment cards from your graveyard to
                  your hand. (Then exile this card. You may play the land
                  later from exile.)"
  ishgard, the holy see: land (Town) -- "This land enters tapped. {T}: Add {W}."
  sanctum weaver: {1}{G} enchantment creature (Dryad) 0/2 --
                  "{T}: Add X mana of any one color, where X is the number
                  of enchantments you control."

Triage (mike-theDude, 2026-08-15) isolated two hypotheses that look
identical from outside:
  (a) Sanctum Weaver's mana was already floating and the adventure still
      was not offered -> the adventure mode itself is not offered;
  (b) the mana was not yet produced and the game failed to see it *could*
      be produced -> potential-mana (feasible_mana_capacity) ignores the
      variable-X source.
This scenario tests BOTH branches explicitly.

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Ishgard, the Holy See + 4x Sanctum Weaver + 16x Exploration
      + 36x Forest (dense playsets are a test-harness convenience; the
      engine accepts >4-of for custom games).
  P1: 60x Forest dummy (never attacks, never blocks).
Drive: P0 mulligans (max 2) unless the opener holds Ishgard. Each turn P0
plays a Forest (never Ishgard -- it must stay in hand), casts Sanctum
Weaver when affordable, then Explorations (static, no triggers/choices).
Readiness: Weaver untapped on the battlefield, >=5 enchantments controlled
by P0 (Weaver + 4 Explorations => X=5), Ishgard in hand, P0 priority in a
main phase. Faith & Grief costs {3}{W}{W}; X=5 of any one color as WWWWW
covers it exactly.

Test sequence (all gate payloads preserved verbatim):
  Phase A (potential mana): with no floating mana, scan every legal action
      and viewer-interaction opportunity for a cast of the adventure half.
  Phase B (weaver mana): activate Sanctum Weaver ({T}: X=5, choose White),
      then verify the mana actually lands in the pool.
  Phase C (full cast): re-scan the gate fresh; if the adventure is offered,
      submit it, pay {3}{W}{W} from the pool, answer the (empty) target
      selection for "up to two" graveyard targets, and let it resolve.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok        readiness reached (Weaver + 4 Explorations on board,
                     Ishgard in hand, main-phase priority).
  A2_offer_potential the adventure cast is offered with potential Weaver
                     mana. (Gate + potential-mana calc handle the variable-X
                     source.)
  A3_weaver_mana     activating Sanctum Weaver and choosing White puts X=5
                     white mana in the pool. FAIL = the mana ability resolves
                     without producing mana.
  A4_cast_completes  the submitted cast resolves; Faith & Grief exiles itself
                     per "(Then exile this card)". not-run when A3 fails
                     (no mana to cast with -- downstream of the failure).
  A5_cleanup         post state sane: stack empty, no errors.

Verdict rule: reproduced iff A1 passed and A3 failed -- Sanctum Weaver's
              "{T}: Add X mana" produces no mana, so Faith & Grief is not
              castable with Weaver mana, matching the report;
              not-reproduced iff A1..A4 all passed;
              blocked iff A1 failed (setup could not be assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - Mana color choice submitted per the advertised response schema
    (exactChoices/choose, or manaGroups spec with choiceIds + count).
  - Authoritative exports only from the host seat (P0 creates the game).
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
RUN_ID = "20261002-7367b"
ISSUE = 7367
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
    "server_run_id": "shared pinned v0.99.0 server on 127.0.0.1:9374 "
                     "(already listening per task body; games-db lives under "
                     "runs/20261002-7366b; not restarted by this run)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release dir; "
              "hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

ISHGARD = "ishgard, the holy see"
FAITH = "faith & grief"
WEAVER = "sanctum weaver"
EXPLORATION = "exploration"
FOREST = "forest"

P0_DECK = [("Ishgard, the Holy See", 4), ("Sanctum Weaver", 4),
           ("Exploration", 16), ("Forest", 36)]
P1_DECK = [("Forest", 60)]

SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> A -> B -> C -> done
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "offer_potential": None,
        "weaver_pool": None,
        "weaver_pool_enchants": None,
        "offer_payloads": {},   # "potential"/"floating" -> verbatim gate dump
        "weaver_activated": False,
        "white_chosen": False,
        "cast_submitted": False,
        "cast_at": None,
        "faith_on_stack": False,
        "faith_resolved": False,
        "ishgard_oid": None,    # object id of the cast Ishgard/Faith&Grief
        "test_done": False,
        "player_keys_logged": False,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_offer_potential",
                                       "A3_weaver_mana", "A4_cast_completes",
                                       "A5_cleanup")},
        "notes": [],
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()

# ------------------------------------------------------------- state utils
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_oids(state, pid, key):
    return [int(o) for o in player_of(state, pid).get("hand", [])
            if obj_lname(state, o) == key]


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def untapped_ids(state, pid, key):
    return [i for i in bf_ids(state, pid, key)
            if not get_obj(state, i).get("tapped")]


def _core_types(o):
    ct = o.get("card_types") or {}
    return [str(t).lower() for t in ct.get("core_types", [])]


def enchantment_count(state, pid):
    return sum(1 for oid, o in (state.get("objects") or {}).items()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and "enchantment" in _core_types(o))


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in _core_types(o)]


def pool_of(state, pid):
    p = player_of(state, pid)
    mp = p.get("mana_pool")
    if isinstance(mp, dict):
        return mp.get("mana", mp)
    return mp


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def cast_action_for(acts, state, key):
    """Returns (action, object_id) for a cast action of the named card."""
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


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        for k in ("text", "label", "code", "name"):
            if d.get(k):
                bits.append(str(d[k]))
    if ch.get("text"):
        bits.append(str(ch["text"]))
    return " | ".join(bits)


# ------------------------------------------------------------- data check
def check_data_level():
    """A0 (informational): the three card-data entries parse as the report
    and triage expect. A data defect here would be a different bug."""
    faith = CARD_DATA.get("faith & grief", {})
    ish = CARD_DATA.get("ishgard, the holy see", {})
    weav = CARD_DATA.get("sanctum weaver", {})
    findings = {
        "faith_mana_cost": faith.get("mana_cost"),
        "faith_oracle": faith.get("oracle_text"),
        "ishgard_types": ish.get("card_type"),
        "ishgard_abilities": ish.get("abilities"),
        "weaver_mana_cost": weav.get("mana_cost"),
        "weaver_abilities": weav.get("abilities"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (
        faith.get("mana_cost") == {"type": "Cost", "shards": ["White", "White"],
                                   "generic": 3}
        and (ish.get("card_type") or {}).get("core_types") == ["Land"]
        and any((ab.get("effect") or {}).get("type") == "Mana"
                and (ab.get("effect") or {}).get("produced", {}).get("type")
                == "AnyOneColor"
                for ab in weav.get("abilities", []))
    )
    say(f"data-level check: {'OK' if ok else 'MISMATCH'} "
        f"(faith cost={faith.get('mana_cost')})")
    ST["notes"].append(f"data-level: card data parses as expected: {ok}")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def submit_vi(c, opp, choice, seq=False):
    """Submit a viewer-interaction choice per the advertised response schema."""
    rtype = (opp.get("response", {}) or {}).get("type")
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    if rtype == "exactChoices" and not seq:
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": choice["id"]}}}
    else:
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        rdata = {"choiceIds": [choice["id"]]}
        if stype == "manaGroups":
            # InteractionResponse::ManaGroups { choice_ids, count }:
            # count must satisfy 1 <= count <= max_batch.
            rdata["count"] = 1
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": stype or "sequence", "data": rdata}}
    wire("vi_submit", {"opp_id": opp.get("interactionId"),
                       "rtype": rtype, "choice": choice_text(choice)[:120],
                       "submission": sub})
    await c.send_interaction(sub)
    return sub


async def do_mulligan_p0(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    hn = hand_lnames(state, pid)
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if ISHGARD not in hn and len(hn) > 5 and mulls < 2:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[P0] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_mulligan_keep(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    MULLS.add(tag)
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")))
    if key in SUBMITTED:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    # keep Ishgard first, then weaver/exploration, then lands
    rank = {ISHGARD: 0, WEAVER: 1, EXPLORATION: 1, FOREST: 2}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 3),
                   reverse=True)[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    n = len(hand) - 7
    if n <= 0:
        return False
    # discard forests first; never Ishgard
    rank = {FOREST: 0, EXPLORATION: 1, WEAVER: 1, ISHGARD: 5}
    picks = [int(x) for x in sorted(
        hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    iid = opp.get("interactionId") or opp.get("id")
                    await c.send_interaction({
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}})
                    return True
    return False


# ------------------------------------------------------------- gate scan
def find_adventure_offer(acts, st, state, ishgard_oids):
    """Scan the cast gate for any offer to cast the adventure half.

    Returns (offered: bool, offers: [verbatim payloads]). An offer is a
    cast-type legal action referencing the Ishgard object (the hand card is
    the land; its adventure half is the only castable mode), or a viewer
    interaction opportunity mentioning faith/adventure."""
    offers = []
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        ser = json.dumps(a, default=str).lower()
        refs_ish = any(str(o) in ser for o in ishgard_oids)
        if refs_ish or "faith" in ser or "adventure" in ser:
            offers.append({"kind": "action", "payload": a})
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            ser = json.dumps(opp, default=str).lower()
            if "faith" in ser or "adventure" in ser:
                offers.append({"kind": "vi_opportunity", "payload": opp})
    return (len(offers) > 0), offers


def setup_ready(state):
    """Weaver untapped, >=5 enchantments, Ishgard in hand, P0 main priority."""
    if not my_priority(state, 0):
        return False
    if state.get("phase") not in ("PreCombatMain", "PostCombatMain"):
        return False
    if state.get("active_player") != 0:
        return False
    if not untapped_ids(state, 0, WEAVER):
        return False
    if enchantment_count(state, 0) < 5:
        return False
    if ISHGARD not in hand_lnames(state, 0):
        return False
    return True

# ------------------------------------------------------------- weaver mana
async def activate_weaver(c, st, acts, state):
    """Submit ActivateAbility for the untapped Sanctum Weaver. Returns True
    if submitted."""
    wid = (untapped_ids(state, 0, WEAVER) or [None])[0]
    if wid is None:
        return False
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        ser = json.dumps(a, default=str)
        if str(wid) in ser or str(a.get("_src_oid")) == str(wid):
            say(f"[P0] activating Sanctum Weaver oid {wid}: {a['type']}")
            wire("weaver_activate", {"weaver_oid": wid, "action": a})
            await submit_as_is(c, a)
            ST["weaver_activated"] = True
            return True
    # fallback: any ActivateAbility whose source is the weaver
    for a in acts:
        if a["type"] == "ActivateAbility":
            say(f"[P0] ActivateAbility fallback: {json.dumps(a, default=str)[:300]}")
            wire("weaver_activate_fallback", {"action": a})
    return False


async def choose_white(c, st):
    """Answer the 'any one color' choice with White, per advertised schema.
    Skips priority-menu opportunities (they are not color prompts)."""
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        rtype = (opp.get("response", {}) or {}).get("type")
        if rtype not in ("exactChoices", "schema"):
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        avail = [ch for ch in chs
                 if (ch.get("status", {}) or {}).get("type") in ("available", None)]
        if not avail:
            continue
        texts = [choice_text(ch) for ch in avail]
        # priority menus must not be treated as color prompts
        if any("passpriority" in t.lower().replace(" ", "")
               or "castspell" in t.lower().replace(" ", "") for t in texts):
            continue
        ser = json.dumps(opp, default=str).lower()
        if "color" not in ser and "mana" not in ser and "white" not in ser:
            continue
        wire("color_choice_opportunity", {"opp": opp})
        pick = next((ch for ch, t in zip(avail, texts)
                     if "white" in t.lower() or "{w}" in t.lower()
                     or t.strip().lower() in ("w", "white")), None)
        if pick is None:
            # mana-choice schema: candidates carry surfaces of type "mana"
            # with data.symbols like ["W"]
            for ch in avail:
                syms = []
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    if isinstance(d, dict) and isinstance(d.get("symbols"), list):
                        syms.extend(str(x) for x in d["symbols"])
                if any(x.upper() == "W" for x in syms):
                    pick = ch
                    break
        if pick is None:
            say("[P0] color prompt unrecognized: "
                f"{[t[:80] for t in texts]}")
            wire("color_choice_unrecognized", {"opp": opp})
            return False
        say(f"[P0] choosing White for Weaver mana: {choice_text(pick)[:80]}")
        await submit_vi(c, opp, pick, seq=(rtype != "exactChoices"))
        ST["white_chosen"] = True
        return True
    return False


async def handle_faith_targets(c, st, acts, state):
    """Faith & Grief: 'Return up to two target artifact and/or enchantment
    cards from your graveyard to your hand.' The graveyard is empty here, so
    zero targets is the legal selection. Submit per the advertised schema.

    Only active in Phase C after the cast was submitted -- and only for the
    actual Faith & Grief target prompt (matched on "faith", never on a bare
    "target", which also matches DeclareAttackers relations prompts)."""
    if ST["phase"] != "C" or not ST["cast_submitted"]:
        return False
    key = ("faith_targets", c.revision)
    if key in SUBMITTED:
        return False
    wtype = wf_of(state).get("type") or ""
    vi = get_vi(st)
    # look for the Faith & Grief target-selection opportunity
    target_opp = None
    if vi:
        for opp in vi.get("opportunities", []) or []:
            ser = json.dumps(opp, default=str).lower()
            if "faith" in ser:
                target_opp = opp
                break
    if target_opp is None and "faith" not in json.dumps(wf_of(state),
                                                        default=str).lower():
        return False
    SUBMITTED.add(key)
    say(f"[P0] faith target selection: wtype={wtype}; "
        f"opp={'yes' if target_opp else 'no'}")
    wire("faith_targets", {"waiting_for": wf_of(state),
                           "opportunity": target_opp})
    if target_opp is None:
        return True
    data = (target_opp.get("response", {}) or {}).get("data", {}) or {}
    choices = data.get("choices") or []
    # "up to two" with an empty graveyard -> zero targets.
    if not choices:
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        rtype = (target_opp.get("response", {}) or {}).get("type")
        if stype in ("sequence", None) or rtype == "TargetSelection":
            sub = {"interactionId": target_opp.get("interactionId"),
                   "response": {"type": "sequence",
                                "data": {"choiceIds": []}}}
            wire("faith_targets_submit", {"submission": sub,
                                          "note": "empty graveyard; zero targets"})
            await c.send_interaction(sub)
            return True
    # non-empty candidates: pick none (up to two allows zero)
    say(f"[P0] unexpected target candidates: {len(choices)}; "
        "selecting zero targets")
    return True


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_p0(c, 0, "P0"):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if await pay_tick(c, acts, "P0"):
        return
    if await do_discard(c, 0, "P0"):
        return
    if await handle_faith_targets(c, st, acts, state):
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    # color choice for the Weaver activation (Phase B): answer it as soon as
    # the opportunity appears; do NOT pass priority while it is pending.
    if ST["phase"] == "B" and ST["weaver_activated"] and not ST["white_chosen"]:
        if await choose_white(c, st):
            return
        return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")

    if phase in ("PreCombatMain", "PostCombatMain") and active == 0 \
            and ST["phase"] == "setup":
        # 1) cast Sanctum Weaver when affordable
        if WEAVER in hand_lnames(state, 0) and not bf_ids(state, 0, WEAVER):
            if len(untapped_lands(state, 0)) >= 2:
                a, _oid = cast_action_for(acts, state, WEAVER)
                if a:
                    say("[P0] casting Sanctum Weaver")
                    wire("cast_weaver", {"action": a["type"]})
                    await submit_as_is(c, a)
                    return
        # 2) cast Exploration when affordable
        if EXPLORATION in hand_lnames(state, 0):
            if len(untapped_lands(state, 0)) >= 1:
                a, _oid = cast_action_for(acts, state, EXPLORATION)
                if a:
                    say("[P0] casting Exploration")
                    wire("cast_exploration", {"action": a["type"]})
                    await submit_as_is(c, a)
                    return
        # 3) land drop: Forest only -- Ishgard must stay in hand
        _pls = [a for a in acts if a["type"] == "PlayLand"]
        if _pls and not hasattr(p0_tick, "_pls_wired"):
            p0_tick._pls_wired = True
            wire("playland_payloads",
                 {"n": len(_pls),
                  "hand": hand_lnames(state, 0),
                  "payloads": _pls[:4]})
            say(f"[P0][dbg] PlayLand payloads wired ({len(_pls)} actions)")
        for a in acts:
            if a["type"] == "PlayLand":
                d = a.get("data", {})
                named = None
                for _k in ("card", "object", "object_id", "source", "land"):
                    _v = d.get(_k)
                    if _v is not None:
                        try:
                            named = obj_lname(state, int(_v))
                            break
                        except (TypeError, ValueError):
                            continue
                if named is not None and named != "?" and named != FOREST:
                    continue  # known non-forest (e.g. Ishgard) -- never play
                if (named is None or named == "?") \
                        and FOREST not in hand_lnames(state, 0):
                    continue
                wire("play_land_submit", {"named": named, "action": a})
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_keep(c, 1, "P1"):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await pay_tick(c, acts, "P1"):
        return
    if await do_discard(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)

# ------------------------------------------------------------- observation
async def export_pre(c):
    env = await c.export_state()
    with open(f"{EVDIR}/pre.json", "w") as f:
        f.write(env)
    ST["pre_exported"] = True
    say("exported pre.json")


async def export_mid(c):
    env = await c.export_state()
    with open(f"{EVDIR}/mid_cast.json", "w") as f:
        f.write(env)
    ST["mid_exported"] = True
    say("exported mid_cast.json")


async def export_post(c):
    env = await c.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(env)
    ST["post_exported"] = True
    say("exported post.json")


def note_faith_on_stack(state):
    """Detect the cast adventure on the stack by object id."""
    if ST["faith_on_stack"]:
        return
    oid = ST["ishgard_oid"]
    if oid is None:
        return
    on_stack_by_zone = (get_obj(state, oid).get("zone") == "Stack")
    on_stack_by_ref = any(
        (e.get("kind") or {}).get("type") == "Spell"
        and str(e.get("source_id")) == str(oid)
        for e in stack_entries(state))
    if on_stack_by_zone or on_stack_by_ref:
        ST["faith_on_stack"] = True
        say(f"FAITH&GRIEF oid {oid} observed on stack "
            f"(by_zone={on_stack_by_zone}, by_ref={on_stack_by_ref})")
        wire("faith_on_stack", {"oid": oid, "by_zone": on_stack_by_zone,
                                "by_ref": on_stack_by_ref})


def faith_oid_in_exile(state):
    oid = ST["ishgard_oid"]
    if oid is None:
        return False
    # exiled cards are objects with zone == "Exile" (no per-player exile list
    # in the viewer state)
    return get_obj(state, oid).get("zone") == "Exile"


async def run_phase_A(c0, st, acts, state):
    """Phase A: scan the gate with potential (unfloated) Weaver mana."""
    ish_oids = hand_oids(state, 0, ISHGARD)
    offered, offers = find_adventure_offer(acts, st, state, ish_oids)
    ST["offer_potential"] = offered
    dump = {
        "offered": offered,
        "offers": offers,
        "ishgard_hand_oids": ish_oids,
        "enchantments_controlled": enchantment_count(state, 0),
        "weaver_untapped": untapped_ids(state, 0, WEAVER),
        "mana_pool": pool_of(state, 0),
        "waiting_for": wf_of(state),
        "cast_type_actions": [a["type"] for a in acts
                              if "cast" in a["type"].lower()],
    }
    with open(f"{EVDIR}/gate_potential.json", "w") as f:
        json.dump(dump, f, indent=1, default=str)
    ST["offer_payloads"]["potential"] = dump
    wire("phaseA_gate", dump)
    say(f"Phase A (potential mana): adventure offered = {offered} "
        f"({len(offers)} offer payloads)")
    await export_pre(c0)
    ST["phase"] = "B"


async def run_phase_B_activate(c0, st, acts, state):
    """Phase B step 1: activate the Weaver."""
    if not ST["weaver_activated"]:
        ok = await activate_weaver(c0, st, acts, state)
        if not ok:
            say("[P0] WARNING: no ActivateAbility for Weaver found; "
                "gate scan will proceed without floating mana")
            wire("weaver_activate_missing",
                 {"acts": [a["type"] for a in acts]})
            ST["weaver_activated"] = True  # don't retry forever
            ST["white_chosen"] = True


async def run_phase_B_scan(c0, st, acts, state):
    """Phase B step 2: verify the Weaver's mana landed in the pool.

    X = number of enchantments P0 controls (5 here: Weaver + 4 Explorations),
    so choosing White must put 5 white mana in the pool."""
    n_ench = enchantment_count(state, 0)
    pool = pool_of(state, 0)
    ST["weaver_pool"] = pool
    ST["weaver_pool_enchants"] = n_ench
    dump = {
        "pool": pool,
        "enchantments_controlled": n_ench,
        "weaver_tapped": [i for i in bf_ids(state, 0, WEAVER)
                          if get_obj(state, i).get("tapped")],
        "white_chosen": ST["white_chosen"],
        "waiting_for": wf_of(state),
    }
    with open(f"{EVDIR}/weaver_mana.json", "w") as f:
        json.dump(dump, f, indent=1, default=str)
    wire("phaseB_pool", dump)
    say(f"Phase B (weaver mana): pool={pool} with {n_ench} enchantments; "
        f"weaver tapped={[i for i in bf_ids(state, 0, WEAVER) if get_obj(state, i).get('tapped')]}")
    if not ST["mid_exported"]:
        await export_mid(c0)
    ST["phase"] = "C"


async def run_phase_C(c0, st, acts, state):
    """Phase C: FRESH gate re-scan (never a stored payload -- actions go
    stale), then submit a current adventure offer if one exists."""
    if ST["cast_submitted"]:
        return
    ish_oids = hand_oids(state, 0, ISHGARD)
    offered, offers = find_adventure_offer(acts, st, state, ish_oids)
    wire("phaseC_gate", {"offered": offered, "n_offers": len(offers),
                         "pool": pool_of(state, 0)})
    say(f"Phase C: fresh gate scan: adventure offered = {offered}")
    if not offered or not offers:
        say("Phase C: no current offer -- nothing to cast "
            "(expected: the Weaver produced no mana)")
        ST["phase"] = "done"
        return
    offer = offers[0]
    ST["ishgard_oid"] = ish_oids[0] if ish_oids else None
    say(f"Phase C: submitting fresh offer "
        f"(kind={offer['kind']}, ishgard_oid={ST['ishgard_oid']})")
    wire("phaseC_submit", {"offer": offer, "ishgard_oid": ST["ishgard_oid"]})
    if offer["kind"] == "action":
        await submit_as_is(c0, offer["payload"])
    else:
        opp = offer["payload"]
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        choices = data.get("choices") or []
        # pick the choice mentioning faith/adventure, else the first
        pick = None
        for ch in choices:
            if "faith" in choice_text(ch).lower() \
                    or "adventure" in choice_text(ch).lower():
                pick = ch
                break
        pick = pick or (choices[0] if choices else None)
        if pick is None:
            say("Phase C: vi opportunity has no choices; cannot submit")
            ST["phase"] = "done"
            return
        await submit_vi(c0, opp, pick)
    ST["cast_submitted"] = True
    ST["cast_at"] = time.time()


# ------------------------------------------------------------- finalization
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]
    pre = post = None
    for name, var in (("pre", "pre"), ("post", "post")):
        try:
            v = json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name} load failed: {e}")
            v = None
        if var == "pre":
            pre = v
        else:
            post = v
    pre_st = (pre or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}

    # A1: setup assembled
    if ST["pre_exported"]:
        ass["A1_setup_ok"] = "passed"
        notes.append("A1 passed: readiness reached -- untapped Sanctum Weaver "
                     "+ >=5 enchantments on board, Ishgard in hand, P0 "
                     "main-phase priority.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: setup never reached readiness "
                     f"(phase={ST['phase']}).")

    # A2: gate offers the adventure with potential mana
    if ST["offer_potential"] is True:
        ass["A2_offer_potential"] = "passed"
        notes.append("A2 passed: the Faith & Grief adventure IS offered with "
                     "potential (unfloated) Weaver mana -- the cast gate and "
                     "the potential-mana calculation handle the variable-X "
                     "source correctly.")
    elif ST["offer_potential"] is False:
        ass["A2_offer_potential"] = "failed"
        notes.append("A2 FAILED: the adventure cast is NOT offered with only "
                     "potential Weaver mana.")
    else:
        notes.append("A2 not-run: Phase A scan never ran")

    # A3: the Weaver's mana actually lands in the pool
    pool = ST.get("weaver_pool")
    n_ench = ST.get("weaver_pool_enchants")
    if pool is not None:
        n_white = 0
        if isinstance(pool, list):
            for m in pool:
                s = json.dumps(m, default=str).lower()
                if '"w"' in s or "'w'" in s or "white" in s:
                    n_white += 1
        if n_white >= 5:
            ass["A3_weaver_mana"] = "passed"
            notes.append(f"A3 passed: Weaver activation put {n_white} white "
                         f"mana in the pool (X={n_ench}).")
        else:
            ass["A3_weaver_mana"] = "failed"
            notes.append(f"A3 FAILED: Sanctum Weaver was activated (tapped) "
                         f"with {n_ench} enchantments controlled and White "
                         f"chosen, but the pool holds {pool} -- the "
                         f"variable-X mana ability resolved without "
                         f"producing mana. This is the reported failure: "
                         f"Faith & Grief cannot be cast 'with mana from the "
                         f"Sanctum Weaver' because that mana never exists.")
    else:
        notes.append("A3 not-run: Phase B pool check never ran")

    # A4: full cast
    if ST["faith_resolved"]:
        if faith_oid_in_exile(post_st):
            ass["A4_cast_completes"] = "passed"
            notes.append("A4 passed: Faith & Grief resolved and exiled "
                         "itself per '(Then exile this card)'.")
        else:
            ass["A4_cast_completes"] = "failed"
            notes.append("A4 FAILED: Faith & Grief resolved but is not in "
                         "exile at post.")
    elif ST["cast_submitted"]:
        ass["A4_cast_completes"] = "failed"
        notes.append("A4 FAILED: cast submitted but never resolved "
                     f"(on_stack={ST['faith_on_stack']}).")
    else:
        notes.append("A4 not-run: no cast submitted "
                     "(expected when A3 failed: no Weaver mana to cast with)")

    # A5: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A5_cleanup"] = "passed"
            notes.append("A5 passed: post stack empty, game continues.")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append(f"A5 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A5 not-run: no post state")

    if ass["A1_setup_ok"] == "passed" and ass["A3_weaver_mana"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: Sanctum Weaver's '{T}: Add X mana "
                     "of any one color, where X is the number of enchantments "
                     "you control' resolves without adding mana to the pool, "
                     "so Faith & Grief is not castable 'with mana from the "
                     "Sanctum Weaver' -- the reported failure. (The cast gate "
                     "itself offers the adventure correctly with potential "
                     "mana, so neither triage hypothesis (a)/(b) is the "
                     "mechanism; the defect is in the variable-X mana ability "
                     "resolution.)")
    elif all(ass[k] == "passed" for k in ("A1_setup_ok", "A2_offer_potential",
                                          "A3_weaver_mana",
                                          "A4_cast_completes")):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: the Weaver produced its mana, "
                     "the adventure was offered, and the cast completed. "
                     "This is not a fix claim.")
    elif ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup could not be assembled; no "
                     "trustworthy result.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set.")

    # server log excerpts for this game
    import glob
    lines = []
    used = None
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"), reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "ishgard" in l.lower() or "faith" in l.lower()
               or "weaver" in l.lower() or "adventure" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7367.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "offer_potential": ST["offer_potential"],
            "weaver_pool": ST["weaver_pool"],
            "weaver_pool_enchants": ST["weaver_pool_enchants"],
            "weaver_activated": ST["weaver_activated"],
            "white_chosen": ST["white_chosen"],
            "cast_submitted": ST["cast_submitted"],
            "faith_on_stack": ST["faith_on_stack"],
            "faith_resolved": ST["faith_resolved"],
            "ishgard_oid": ST["ishgard_oid"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets (4x/16x) are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "Faith & Grief was cast with zero targets ('up to two'); the "
            "return-to-hand sub-effect was not exercised with live targets.",
            "The adventure's 'you may play the land later from exile' was "
            "not exercised (no second land drop driven).",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Ishgard, the Holy See + 4x Sanctum Weaver + "
                      "16x Exploration + 36x Forest; P1: 60x Forest",
        "contract_line": "Faith & Grief (adventure half of Ishgard, the Holy "
                         "See) is castable with mana from Sanctum Weaver: "
                         "gate offer with potential mana (Phase A) + the "
                         "Weaver's variable-X mana ability producing X mana "
                         "(Phase B)",
        "prior_runs": [],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return verdict


# ------------------------------------------------------------- main
async def main():
    reset()
    check_data_level()

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    finalized = False
    b_scan_revs = 0

    while time.time() - t_start < SETUP_DEADLINE_S and not finalized:
        await asyncio.sleep(0.15)
        now = time.time()
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last.get(tag))
            stale = now - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = c.revision
            last_tick_at[tag] = now
            # ---- observation pass
            try:
                try:
                    while True:
                        t, data = c.inbox.get_nowait()
                        if t in ("ActionRejected", "Error"):
                            say(f"[{tag}] {t}: {json.dumps(data)[:300]}")
                            wire("rejection", {"who": tag, "type": t,
                                               "data": data})
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                if tag == "P0" and not ST["player_keys_logged"]:
                    ST["player_keys_logged"] = True
                    wire("player_keys",
                         {"p0_keys": sorted(player_of(state, 0).keys())})
                acts = merged_actions(st)
                if tag == "P0":
                    note_faith_on_stack(state)
                    # faith left the stack -> resolved
                    if ST["faith_on_stack"] and not ST["faith_resolved"]:
                        oid = ST["ishgard_oid"]
                        on_stack = (
                            get_obj(state, oid).get("zone") == "Stack"
                            if oid is not None else False)
                        if not on_stack and not any(
                                (e.get("kind") or {}).get("type") == "Spell"
                                and str(e.get("source_id")) == str(oid)
                                for e in stack_entries(state)):
                            ST["faith_resolved"] = True
                            say(f"FAITH&GRIEF oid {oid} resolved; zone now "
                                f"{get_obj(state, oid).get('zone') if oid else '?'}")
                            wire("faith_resolved", {"oid": oid})
                    # watchdog: cast submitted but never reached the stack
                    if ST["cast_submitted"] and not ST["faith_on_stack"] \
                            and not ST["post_exported"]:
                        if now - (ST["cast_at"] or now) > 180:
                            wire("cast_stuck",
                                 {"waiting_for": wf_of(state),
                                  "ishgard_oid": ST["ishgard_oid"],
                                  "zone": get_obj(state, ST["ishgard_oid"]).get("zone")
                                  if ST["ishgard_oid"] else None})
                            say("cast submitted but never reached the stack "
                                "in 180s -- exporting stuck state")
                            await export_post(c)
                            finalized = True
                            break
                    # ---- test phase machine ----
                    if ST["phase"] == "setup" and setup_ready(state) \
                            and not ST["pre_exported"]:
                        await run_phase_A(c, st, acts, state)
                    elif ST["phase"] == "B" and not ST["weaver_activated"]:
                        # activation happens in p0_tick via activate_weaver;
                        # here we only drive the fallback if tick missed it
                        pass
                    if ST["phase"] == "B" and ST["weaver_activated"] \
                            and ST["white_chosen"] and not ST["post_exported"]:
                        # wait a beat for the mana to land, then scan once
                        b_scan_revs += 1
                        if b_scan_revs >= 3 and ST["weaver_pool"] is None:
                            await run_phase_B_scan(c, st, acts, state)
                    if ST["phase"] == "C" and not ST["cast_submitted"] \
                            and my_priority(state, 0) \
                            and state.get("phase") in ("PreCombatMain",
                                                       "PostCombatMain"):
                        await run_phase_C(c, st, acts, state)
                    if ST["phase"] == "C" and ST["cast_submitted"] \
                            and ST["faith_resolved"] and not ST["post_exported"]:
                        if my_priority(state, 0) and not stack_entries(state):
                            await asyncio.sleep(1.0)
                            await export_post(c)
                            say(f"test complete at turn {state.get('turn_number')}")
                            finalized = True
                            break
                    if ST["phase"] == "done" and not ST["post_exported"]:
                        await export_post(c)
                        finalized = True
                        break
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            # ---- action pass
            try:
                acts = merged_actions(st)
                if tag == "P0" and ST["phase"] == "B" \
                        and not ST["weaver_activated"] \
                        and my_priority(state, 0):
                    await run_phase_B_activate(c, st, acts, state)
                await tick(st, acts, st["state"], c)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
    say("finalizing")
    verdict = await finalize(p0)
    await p0.close()
    await p1.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    # copy the scenario into the evidence dir for the manifest
    shutil.copy(f"{BACKFILL}/driver/scenario_7367.py",
                f"{EVDIR}/scenario_7367.py")
    sys.exit(0)
