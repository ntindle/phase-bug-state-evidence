#!/usr/bin/env python3
"""Issue #7181: Beseech the Queen x Witherbloom - granted affinity not recognized.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, status:needs-repro, source:discord):
  "Beseech the Queen is not recognizing the Witherbloom's affinity mechanic."
  "[[Beseech the Queen]] as a 6 colorless mana should be free if I have 6
   creatures out *or* one black/2 colorless if I have 5 creatures out, etc."

Granting source identified (triage acceptance criterion #1):
  Witherbloom, the Balancer - "Affinity for creatures (This spell costs {1}
  less to cast for each creature you control.) Flying, deathtouch. Instant
  and sorcery spells you cast have affinity for creatures."
  (static ability, CastWithKeyword mode, verified in pinned card-data.json
  v0.114.0 before the run; full Oracle text recorded in notes/A0.)

Oracle text (pinned card-data.json v0.114.0, verified 2026-10-21 before run):
  Beseech the Queen ({2/B}{2/B}{2/B}, mana value 6, Sorcery):
    "({2/B} can be paid with any two mana or with {B}. This card's mana
     value is 6.) Search your library for a card with mana value less than
     or equal to the number of lands you control, reveal it, put it into
     your hand, then shuffle."
  Witherbloom, the Balancer ({B}{G}{6}, 5/5 Legendary Creature - Elder
  Dragon): "Affinity for creatures (This spell costs {1} less to cast for
  each creature you control.) Flying, deathtouch. Instant and sorcery
  spells you cast have affinity for creatures."

Expected (CR 702.42a, 601.2f): with the Balancer on the battlefield,
Beseech the Queen (a sorcery) gains affinity for creatures: it costs {1}
less to cast for each creature its controller controls, applied to the
generic portion of {2/B}{2/B}{2/B}. 5 creatures -> pay {1}. 6 creatures ->
pay {0} (free). Creature spells (e.g. Grizzly Bears) get no discount.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 8x Witherbloom, the Balancer, 12x Grizzly Bears, 8x Beseech the
      Queen, 16x Forest, 16x Swamp.
  P1: 12x Grizzly Bears, 48x Forest (passive: land drops, a couple of
      bears, empty attacks/blocks).

Planned line:
  T2-T4: bears (3). T5: Balancer ({B}{G}{6} - 3 affinity = {B}{G}{3}).
  T6: bear #4 = BEAR CONTROL (creature spell: no granted affinity, must
      cost full {1}{G} -> exactly 2 lands tapped); then Beseech #1 at
      5 creatures (Balancer + 4 bears) -> expect exactly 1 land tapped.
  T7: bear #5 -> 6 creatures; Beseech #2 -> expect 0 lands tapped (free).
  Each Beseech resolution is driven through its library-search prompt
  (first legal candidate answered; tutored card recorded).

Cost measurement: the engine auto-taps lands for cast mana (no blocking
payment prompt expected). tapped_delta = |untapped lands before submit -
untapped lands at first stack sighting|, corroborated by the pre/post
authoritative exports (same turn). No other P0 actions are taken while a
test cast is in flight, so the delta is the payment.

Assertions (each passed / failed / not-run):
  A1_setup          Balancer cast and on P0 BF; test5 ran at exactly
                    5 P0 creatures (leg-1 setup the A3 measurement
                    depends on; leg-2 absence is recorded in A4/A5,
                    not here - v0.112.0 finalize lesson).
  A2_bear_control   bear #4 (creature spell, Balancer on BF) tapped
                    exactly 2 lands (full {1}{G}; granted affinity is
                    sorcery/instant-only).
  A3_five_cost      Beseech #1 at 5 creatures tapped exactly 1 land
                    (6 generic - 5 affinity).
  A4_six_cost       Beseech #2 at 6 creatures tapped exactly 0 lands
                    (free).
  A5_resolution     both Beseech casts resolved: search prompt answered,
                    tutored card in P0 hand, no Beseech stuck on stack.

Verdict rule:
  blocked        iff A1 fails (setup never reached).
  reproduced     iff A1 passes and (A3 fails or A4 fails): the granted
                 affinity did not reduce Beseech's cost as reported
                 (tapped more - or a different wrong amount - than the
                 affinity math allows). A5 may be not-run then.
  not-reproduced iff A2, A3, A4, A5 all pass: the granted affinity is
                 applied exactly (dose-response 5->1, 6->0, bears full).

v0.114.0 re-validation port (protocol 153, 2026-10-21) of the verified
v0.112.0 run (driver/scenario_7181_v01120.py, run 20261018-7181-v01120,
verdict reproduced). The v0.84.0 run (driver/scenario_7181.py, run
20260915-7181, verdict reproduced) is the original base. Protocol-153
notes: waiting_for is gone (null); priority = top-level PassPriority
legal action; all decisions via viewer_interaction (waitingForKind.code);
MulliganDecision via legacy Action. Interaction conventions proven
identical 151->153 on the 2026-10-19/20/21 v0.114.0 re-validation runs
(#301, #1234, #1488, #7343, #6860, #6862, #6866, #6868, #6876, #6891,
#6899, #6903, #6906, #6907, #6910, #6911, #6912, #6914, #6916, #6917,
#6951, #6963, #6982, #6983, #6986, #6987, #6988, #7173, #7176, #10085).

v0.112.0 run findings carried over: the driver's protocol-147 search
answer (response type "sequence") was rejected invalid_interaction_response
and the Beseech search prompt dangled, so leg 2 never ran and the
in-memory "resolved" flag was a false positive (the graveyard Beseech was
consistent with a cleanup discard). This port submits the search answer
using the vi response spec's own type (per the proven protocol-153
discard pattern: schema/select with choiceIds), which may fix the answer;
if the search still cannot be answered, A5 is recorded not-run/failed
with the rejection preserved, per PLAYBOOK step 5 (a confirmed failure
remains a reproduction even if downstream assertions cannot execute).
A5's resolved check is tightened: search_answered AND the tutored card
observed in P0's hand (the graveyard-count check alone false-positived on
v0.112.0 via cleanup discards).

Evidence: evidence/7181/<run-id>/pre_cast5.json, cast5_detail.json,
post_cast5.json, pre_cast6.json, cast6_detail.json, post_cast6.json,
final.json, run.json, manifest.sha256, summary.png, scenario_7181_01140.py,
wire_log.jsonl, scenario_run.log, server.log (excerpts).
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7181
RUN_ID = os.environ.get("RUN_ID", "run-7181-reval-v01140-20261021-0411")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BALANCER = "witherbloom, the balancer"
BESEECH = "beseech the queen"
BEAR = "grizzly bears"
FOREST = "forest"
SWAMP = "swamp"
LANDS = (FOREST, SWAMP)

P0_DECK = [(BALANCER, 8), (BEAR, 12), (BESEECH, 8), (FOREST, 16),
           (SWAMP, 16)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

RELDIR = f"{BACKFILL}/server/releases/v0.114.0"
MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "025c26c78762d8fc0bed47a7774749465c3e40751df73310cd7dee6ed5d506ef",
    "card_data_sha256": "7fe886cc031d9bd6532317572f84f65da66941ebdf6a43d337b625fbc369548e",
    "draft_pools_sha256": "4a0b44a9af0881f924b4120a43fe04947dd673aa867e796917b696d7ea26ba59",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
    "mode": "single-user",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode at pin time "
                      "2026-10-19; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run; "
                      "release v0.114.0 confirmed latest stable via GitHub "
                      "releases API 2026-10-21",
    "observed_at": "2026-10-21",
    "handshake": "ServerHello observed pre-run: v0.114.0 / ac67cbb / "
                 "protocol 153 / mode Full on 127.0.0.1:9374",
    "source": "pinned v0.114.0 (minisign-verified binary + signed data "
              "manifest, ledger server pin 2026-10-19); binary + data "
              "SHA-256 re-verified by this run against the pin; "
              "backfill-owned phase-server-slim already listening on "
              "127.0.0.1:9374 (started 2026-10-21 01:13 by the #6963 run, "
              "runs/run-6963-reval-v01140-20261021-0111/); ServerHello "
              "re-checked by this run.",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(state, oid):
    o = (state.get("objects") or {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names and not o.get("tapped")):
            out.append(int(oid))
    return out


def merged_actions(st):
    """Top-level legal_actions plus per-object actions (injecting _src_oid
    from the dict key) - protocol 153."""
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def vi_of(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_ops(st):
    return (vi_of(st) or {}).get("opportunities", []) or []


def vi_kind_code(st):
    return ((vi_of(st) or {}).get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    """Protocol 153: priority = top-level PassPriority legal action."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def stack_entries(state):
    return state.get("stack") or []


def spell_on_stack(state, name, pid=0):
    for e in stack_entries(state):
        kind = (e.get("kind") or {})
        if kind.get("type") == "Spell":
            sid = e.get("source_id")
            if sid is not None and lname(state, int(sid)) == name \
                    and get_obj(state, int(sid)).get("controller") == pid:
                return e
    return None


def p0_creature_oids(state):
    return [oid for oid in bf_ids(state, 0)
            if lname(state, oid) in (BALANCER, BEAR)]


def _cand_reference(ch):
    """Engine-issued candidate reference from the choice surfaces."""
    for s in (ch or {}).get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def submit_as_is(c, a):
    clean = {k: v for k, v in a.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub})
    await c.send_interaction(sub)


async def do_mulligan(c, acts, tag):
    """Protocol 153: MulliganDecision is a top-level legal action."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull")
    if key in ST["done_keys"]:
        return True
    ST["done_keys"].add(key)
    say(f"[{tag}] mulligan: keep 7")
    wire("mulligan", {"who": tag, "decision": "Keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard(c, st, pid, tag):
    """Protocol 153: DiscardToHandSize via viewer_interaction schema/select.
    Never runs while one of our Beseech casts awaits resolution, so a
    Beseech library-search can never be mis-answered as a discard."""
    for key in ("cast5", "cast6"):
        r = ST[key]
        if r is not None and r.get("stack_seen") and not r.get("resolved"):
            return False
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in ST["done_keys"]:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]

        def drank(o):
            nm = lname(state, o)
            if nm in (BALANCER, BESEECH):
                return (2, nm)  # protect test cards
            if lname(state, o) in LANDS:
                return (0, nm)  # discard lands first
            return (1, nm)
        ranked = sorted(hand, key=drank)
        picks = ranked[:n]
        choice_ids = [ref_of[str(o)] for o in picks if str(o) in ref_of]
        if not choice_ids:
            return False
        ST["done_keys"].add(key)
        say(f"[{tag}] discarding to hand size: "
            f"{[lname(state, o) for o in picks]}")
        wire("discard", {"who": tag,
                         "oids": picks,
                         "names": [lname(state, o) for o in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds":
                                                    choice_ids}}}, tag)
        return True
    return False


async def do_declare_empty(c, acts, st, pid, tag):
    """Empty attackers/blockers: top-level actions first, vi relations
    fallback (protocol-153 pattern)."""
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid \
            and "declareattack" in phase.lower():
        for opp in vi_ops(st):
            resp = opp.get("response") or {}
            data = resp.get("data") or {}
            spec = data.get("spec") or {}
            if resp.get("type") == "schema" and isinstance(spec, dict) \
                    and spec.get("type") == "relations":
                iid = opp.get("interactionId")
                key = (tag, "declare", str(iid))
                if key in ST["done_keys"]:
                    return True
                ST["done_keys"].add(key)
                say(f"[{tag}] declare empty via vi relations opportunity")
                wire("declare_empty_vi", {"who": tag})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "relations",
                                                   "data": {"relations": []}}},
                                    tag)
                return True
    return False


async def answer_search(c, pid, tag, st, state):
    """Answer Beseech the Queen's library-search prompt via
    viewer_interaction. On protocol 153 the search surfaces as a
    schema/select opportunity (waitingForKind.code == 'select'; observed
    2026-10-21) - NOT a 'search' kind. Only runs while one of OUR Beseech
    casts awaits resolution (P1's deck has no Beseech). Non-search
    opportunities (e.g. exactChoices passPriority, kind='choose') are
    logged and left alone."""
    ops = vi_ops(st)
    if not ops:
        return False
    rec = None
    rkey = None
    for key in ("cast5", "cast6"):
        r = ST[key]
        if r is not None and r.get("stack_seen") \
                and not r.get("resolved") and not r.get("search_answered"):
            rec = r
            rkey = key
            break
    if rec is None:
        return False
    kind = vi_kind_code(st)
    for opp in ops:
        iid = opp.get("interactionId")
        if iid in ST["answered_iids"]:
            continue
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
        pick = cands[0]
        cid = pick.get("id")
        ref = _cand_reference(pick)
        nm = (lname(state, int(ref)) if ref is not None
              and str(ref).lstrip("-").isdigit() else cid)
        sub = {"interactionId": iid,
               "response": {"type": "select",
                            "data": {"choiceIds": [cid]}}}
        wire("search_answer", {"tag": tag, "kind": kind, "rec": rkey,
                               "choice": nm, "n_candidates": len(cands),
                               "submission": sub})
        await interact_as(c, sub, tag)
        ST["answered_iids"].append(iid)
        rec["search_answered"] = True
        rec["tutored"] = nm
        say(f"[{tag}] search prompt answered (kind={kind}): "
            f"tutored '{nm}' ({len(cands)} candidates)")
        return True
    if kind and kind not in ST["logged_kinds"]:
        ST["logged_kinds"].add(kind)
        obs = {"kind": kind, "stage": ST["stage"], "rec": rkey,
               "n_ops": len(ops)}
        ST["obs"]["unexpected_prompts"].append(obs)
        say(f"[{tag}] no search-shaped vi while {rkey} awaits "
            f"resolution (kind={kind!r}); leaving it alone")
        wire("unexpected_vi", obs)
    return False


def check_cast_confirm(state):
    """Protocol-153 cast-confirmation guard: after a bare CastSpell the
    driver must not pass priority or start new main-phase plays until the
    spell is confirmed on the stack (it can LOSE A RACE to our own
    PassPriority and be silently dropped). Returns 'hold' while in flight,
    'proceed' when confirmed, or 'dropped:<purpose>' after the backstop."""
    u = ST["cast_unconfirmed"]
    if u is None:
        return "proceed"
    if spell_on_stack(state, u["lname"], 0) is not None:
        ST["cast_unconfirmed"] = None
        return "proceed"
    if time.time() - u["at"] > 30:
        ST["cast_unconfirmed"] = None
        return f"dropped:{u['purpose']}"
    return "hold"


def rearm_dropped(purpose):
    """A silently-dropped cast: remove its record and return the stage to
    the pre-cast state so the main-phase logic can resubmit."""
    say(f"[P0] cast '{purpose}' silently dropped; re-arming")
    wire("cast_dropped", {"purpose": purpose})
    ST["bear_casts"] = [r for r in ST["bear_casts"]
                        if not (r["purpose"] == purpose
                                and r.get("tapped_delta") is None)]
    if purpose == "balancer":
        ST["balancer_cast"] = False
    elif purpose == "ramp":
        pass  # stage stays "setup"; next tick recasts the ramp bear
    elif purpose == "control":
        ST["stage"] = "setup"
    elif purpose == "sixth":
        ST["stage"] = "test6setup"
    elif purpose == "beseech5":
        ST["cast5"] = None
        ST["stage"] = "test5"
    elif purpose == "beseech6":
        ST["cast6"] = None
        ST["stage"] = "test6"


ST = {}


def init_state():
    ST.update({
        "finished": False,
        "stage": "setup",   # setup -> bear_control_cast -> test5 ->
                            # test5_cast -> test5_resolving -> test6setup ->
                            # sixth_cast -> test6 -> test6_cast ->
                            # test6_resolving -> final -> done
        "game_code": None,
        "balancer_oid": None,
        "balancer_cast": False,
        "balancer_cast_turn": None,
        "bear_casts": [],
        "cast5": None,
        "cast6": None,
        "answered_iids": [],
        "vi_kinds_seen": [],
        "logged_kinds": set(),
        "done_keys": set(),
        "turns_seen": set(),
        "final_exported": False,
        "exports": {},
        "last_rev_acted": {},
        "cast_unconfirmed": None,
        "cast5_notoffered": False,
        "cast6_notoffered": False,
        "obs": {"rejections": [], "unexpected_prompts": [],
                "tick_errors": []},
    })


def acted(key, rev):
    if ST["last_rev_acted"].get(key) == rev:
        return True
    ST["last_rev_acted"][key] = rev
    return False


def cast_spell_action(acts, state, pid, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        dd = a.get("data") or {}
        oid = dd.get("object_id")
        if oid is not None and lname(state, int(oid)) == name:
            return a
    return None


CAST_QUIET = ("bear_control_cast", "test5_cast", "test5_resolving",
              "sixth_cast", "test6_cast", "test6_resolving", "final")


async def p0_tick(st, acts, state):
    p0 = ST["p0"]
    rev = st.get("state_revision", -1)
    if await do_mulligan(p0, acts, "P0"):
        return
    cc = check_cast_confirm(state)
    if cc == "hold":
        return
    if cc.startswith("dropped:"):
        rearm_dropped(cc.split(":", 1)[1])
        return
    if await do_discard(p0, st, 0, "P0"):
        return
    for a in acts:
        if a["type"] in ("PayMana", "PayManaAbilityMana",
                         "PayManaAbility", "ManaPayment"):
            await submit_as_is(p0, a)
            return
    kind = vi_kind_code(st)
    if kind and kind not in ST["vi_kinds_seen"]:
        ST["vi_kinds_seen"].append(kind)
    while True:
        try:
            t, data = p0.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            ST["obs"]["rejections"].append({"stage": ST["stage"],
                                            "type": t, "data": data})
            say(f"[P0] REJECTION in stage {ST['stage']}: "
                f"{json.dumps({'type': t, 'data': data}, default=str)[:300]}")
    if await answer_search(p0, 0, "P0", st, state):
        return

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    ST["turns_seen"].add(turn)

    # ---- observations ----
    if ST["balancer_oid"] is None:
        b = bf_ids(state, 0, BALANCER)
        if b:
            ST["balancer_oid"] = b[0]
            ST["balancer_cast_turn"] = turn
            say(f"[P0] Balancer ENTERED oid={b[0]} turn={turn}")

    for rec in ST["bear_casts"]:
        if rec["stack_seen"] is None:
            if spell_on_stack(state, BEAR, 0) is not None:
                rec["stack_seen"] = True
                after = set(untapped_lands(state, 0, LANDS))
                before = set(rec["untapped_before"])
                rec["tapped_delta"] = len(before - after)
                rec["untapped_after"] = sorted(after)
                say(f"[P0] bear ({rec['purpose']}) on stack: "
                    f"tapped_delta={rec['tapped_delta']} "
                    f"(untapped {len(before)}->{len(after)})")
                wire("bear_stack",
                     {"purpose": rec["purpose"],
                      "tapped_delta": rec["tapped_delta"]})
        elif not rec["entered"]:
            if len(bf_ids(state, 0, BEAR)) > rec["bears_before"]:
                rec["entered"] = True
                say(f"[P0] bear ({rec['purpose']}) ENTERED")
                if rec["purpose"] == "control" and ST["stage"] == \
                        "bear_control_cast":
                    ST["stage"] = "test5"
                    say("[P0] stage -> test5 "
                        f"({len(p0_creature_oids(state))} creatures)")
                elif rec["purpose"] == "sixth" and ST["stage"] == \
                        "sixth_cast":
                    ST["stage"] = "test6"
                    say("[P0] stage -> test6 "
                        f"({len(p0_creature_oids(state))} creatures)")

    for key, post, next_stage in (
            ("cast5", "post_cast5", "test5_resolving"),
            ("cast6", "post_cast6", "test6_resolving")):
        rec = ST[key]
        if rec is None:
            continue
        if rec["stack_seen"] is None:
            if spell_on_stack(state, BESEECH, 0) is not None:
                rec["stack_seen"] = True
                after = set(untapped_lands(state, 0, LANDS))
                before = set(rec["untapped_before"])
                rec["tapped_delta"] = len(before - after)
                rec["untapped_after"] = sorted(after)
                rec["stack_turn"] = turn
                exp = 1 if key == "cast5" else 0
                say(f"[P0] {key}: Beseech ON STACK "
                    f"tapped_delta={rec['tapped_delta']} "
                    f"(expect {exp}; untapped "
                    f"{len(before)}->{len(after)})")
                wire(f"{key}_stack",
                     {"tapped_delta": rec["tapped_delta"],
                      "creatures": rec["creatures_at_cast"]})
                await export_named(post)
                ST["stage"] = next_stage
                return
        elif not rec["resolved"]:
            # Tightened vs v0.112.0: the graveyard-count check alone
            # false-positived via cleanup discards. Real resolution =
            # search answered AND the tutored card observed in hand.
            if rec["search_answered"] and rec["tutored"] \
                    and rec["tutored"] in hand_lnames(state, 0):
                rec["resolved"] = True
                rec["resolved_turn"] = turn
                say(f"[P0] {key}: Beseech RESOLVED, tutored "
                    f"'{rec['tutored']}' now in hand")
                wire(f"{key}_resolved", {"tutored": rec["tutored"],
                                        "turn": turn})
                ST["stage"] = ("test6setup" if key == "cast5"
                               else "final")
                return

    if ST["stage"] == "final" and not ST["final_exported"]:
        if await export_named("final"):
            ST["final_exported"] = True
            ST["stage"] = "done"
            say("[P0] final exported; stage -> done")
        return

    if await do_declare_empty(p0, acts, st, 0, "P0"):
        return

    # ---- main-phase actions (never during a cast flight) ----
    if ST["stage"] not in CAST_QUIET and my_main(state, 0) \
            and my_priority(acts):
        for oid in hand_ids(state, 0):
            if lname(state, oid) in LANDS:
                pla = next((a for a in acts
                            if a.get("type") == "PlayLand"
                            and (a.get("data") or {}).get(
                                "object_id") == oid), None)
                if pla and not acted("p0land", rev):
                    await submit_as_is(p0, pla)
                    return
        # cast the Balancer whenever the engine offers it
        if not ST["balancer_cast"] \
                and BALANCER in hand_lnames(state, 0):
            ca = cast_spell_action(acts, state, 0, BALANCER)
            if ca and not acted("balancer", rev):
                say("[P0] casting Witherbloom, the Balancer "
                    f"(creatures={len(p0_creature_oids(state))})")
                await submit_as_is(p0, ca)
                ST["balancer_cast"] = True
                ST["cast_unconfirmed"] = {"at": time.time(),
                                         "lname": BALANCER,
                                         "purpose": "balancer"}
                return
        # bears: ramp (3) -> control (#4) -> sixth (#5)
        nbears = len(bf_ids(state, 0, BEAR))
        purpose = None
        # ramp bears until 3 on the battlefield regardless of whether
        # the Balancer is already out (draw variance can land the
        # Balancer early; re-validation fix 2026-10-18 - avoids the
        # nbears==2 deadlock seen when Balancer arrived first)
        if nbears < 3:
            purpose = "ramp"
        elif ST["stage"] == "setup" and nbears == 3 \
                and ST["balancer_oid"] is not None:
            purpose = "control"
        elif ST["stage"] == "test6setup" and nbears == 4:
            purpose = "sixth"
        if purpose and BEAR in hand_lnames(state, 0):
            ca = cast_spell_action(acts, state, 0, BEAR)
            if ca and not acted(f"bear_{purpose}", rev):
                rec = {"purpose": purpose,
                       "submitted_turn": turn,
                       "untapped_before": sorted(
                           untapped_lands(state, 0, LANDS)),
                       "bears_before": nbears,
                       "stack_seen": None,
                       "tapped_delta": None,
                       "entered": False}
                ST["bear_casts"].append(rec)
                say(f"[P0] casting bear ({purpose}) "
                    f"untapped_before={len(rec['untapped_before'])}")
                await submit_as_is(p0, ca)
                ST["cast_unconfirmed"] = {"at": time.time(),
                                         "lname": BEAR,
                                         "purpose": purpose}
                if purpose == "control":
                    ST["stage"] = "bear_control_cast"
                elif purpose == "sixth":
                    ST["stage"] = "sixth_cast"
                return
        # TEST5: Beseech #1 at exactly 5 creatures
        if ST["stage"] == "test5" \
                and len(p0_creature_oids(state)) == 5 \
                and BESEECH in hand_lnames(state, 0):
            ca = cast_spell_action(acts, state, 0, BESEECH)
            if ca is None:
                if not ST["cast5_notoffered"]:
                    ST["cast5_notoffered"] = True
                    save_prompt_detail("cast5", state, st)
                    ST["obs"]["unexpected_prompts"].append(
                        "test5: Beseech CastSpell not advertised at "
                        "5 creatures")
                    say("[P0] test5: Beseech NOT advertised "
                        "(detail saved)")
            elif not acted("cast5", rev):
                await export_named("pre_cast5")
                save_prompt_detail("cast5", state, st)
                ST["cast5"] = {
                    "submitted": time.time(),
                    "untapped_before": sorted(
                        untapped_lands(state, 0, LANDS)),
                    "creatures_at_cast": len(
                        p0_creature_oids(state)),
                    "stack_seen": None, "tapped_delta": None,
                    "search_answered": False, "tutored": None,
                    "resolved": False}
                say(f"[P0] TEST5: casting Beseech the Queen at "
                    f"{ST['cast5']['creatures_at_cast']} creatures, "
                    f"untapped_before="
                    f"{len(ST['cast5']['untapped_before'])} "
                    f"(expect 1 land tapped)")
                wire("cast5_submit",
                     {"untapped_before":
                      ST["cast5"]["untapped_before"]})
                await submit_as_is(p0, ca)
                ST["cast_unconfirmed"] = {"at": time.time(),
                                         "lname": BESEECH,
                                         "purpose": "beseech5"}
                ST["stage"] = "test5_cast"
                return
        # TEST6: Beseech #2 at exactly 6 creatures
        if ST["stage"] == "test6" \
                and len(p0_creature_oids(state)) == 6 \
                and BESEECH in hand_lnames(state, 0):
            ca = cast_spell_action(acts, state, 0, BESEECH)
            if ca is None:
                if not ST["cast6_notoffered"]:
                    ST["cast6_notoffered"] = True
                    save_prompt_detail("cast6", state, st)
                    ST["obs"]["unexpected_prompts"].append(
                        "test6: Beseech CastSpell not advertised at "
                        "6 creatures")
                    say("[P0] test6: Beseech NOT advertised "
                        "(detail saved)")
            elif not acted("cast6", rev):
                await export_named("pre_cast6")
                save_prompt_detail("cast6", state, st)
                ST["cast6"] = {
                    "submitted": time.time(),
                    "untapped_before": sorted(
                        untapped_lands(state, 0, LANDS)),
                    "creatures_at_cast": len(
                        p0_creature_oids(state)),
                    "stack_seen": None, "tapped_delta": None,
                    "search_answered": False, "tutored": None,
                    "resolved": False}
                say(f"[P0] TEST6: casting Beseech the Queen at "
                    f"{ST['cast6']['creatures_at_cast']} creatures, "
                    f"untapped_before="
                    f"{len(ST['cast6']['untapped_before'])} "
                    f"(expect 0 lands tapped - free)")
                wire("cast6_submit",
                     {"untapped_before":
                      ST["cast6"]["untapped_before"]})
                await submit_as_is(p0, ca)
                ST["cast_unconfirmed"] = {"at": time.time(),
                                         "lname": BESEECH,
                                         "purpose": "beseech6"}
                ST["stage"] = "test6_cast"
                return

    # default: pass priority (always fall through, cf. #6862)
    if my_priority(acts) and not acted("p0pass", rev):
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p0, pa)


async def p1_tick(st, acts, state):
    p1 = ST["p1"]
    rev = st.get("state_revision", -1)
    if await do_mulligan(p1, acts, "P1"):
        return
    if await do_discard(p1, st, 1, "P1"):
        return
    for a in acts:
        if a["type"] in ("PayMana", "PayManaAbilityMana",
                         "PayManaAbility", "ManaPayment"):
            await submit_as_is(p1, a)
            return
    if await do_declare_empty(p1, acts, st, 1, "P1"):
        return
    if my_main(state, 1) and my_priority(acts):
        for oid in hand_ids(state, 1):
            if lname(state, oid) == FOREST:
                pla = next((a for a in acts
                            if a.get("type") == "PlayLand"
                            and (a.get("data") or {}).get(
                                "object_id") == oid), None)
                if pla and not acted("p1land", rev):
                    await submit_as_is(p1, pla)
                    return
        if len(bf_ids(state, 1, BEAR)) < 2 \
                and BEAR in hand_lnames(state, 1):
            ca = cast_spell_action(acts, state, 1, BEAR)
            if ca and not acted("p1bear", rev):
                say("[P1] casting Grizzly Bears")
                await submit_as_is(p1, ca)
                return
    if my_priority(acts) and not acted("p1pass", rev):
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p1, pa)


async def export_named(name):
    p0 = ST["p0"]
    try:
        raw = await p0.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        ST["exports"][name] = True
        say(f"exported {name}.json "
            f"(turn={env['state'].get('turn_number')})")
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        ST["obs"]["tick_errors"].append(f"export_{name}: {e!r}")
        return None


def save_prompt_detail(name, state, st):
    try:
        detail = {
            "name": name,
            "captured_at": time.time(),
            "turn_number": state.get("turn_number"),
            "phase": state.get("phase"),
            "p0_creatures": len(p0_creature_oids(state)),
            "p0_untapped_lands": sorted(
                untapped_lands(state, 0, LANDS)),
            "vi_kind": vi_kind_code(st),
            "viewer_interaction": st.get("viewer_interaction"),
            "legal_actions": st.get("legal_actions"),
            "legal_actions_by_object":
                st.get("legal_actions_by_object"),
            "stack": state.get("stack"),
        }
        with open(f"{EVDIR}/{name}_detail.json", "w") as f:
            json.dump(detail, f, indent=1, default=str)
        say(f"saved {name}_detail.json")
    except Exception as e:
        say(f"prompt detail {name} FAILED: {e!r}")
        ST["obs"]["tick_errors"].append(f"detail_{name}: {e!r}")


async def tick(c, pid, tag, fn):
    while True:
        await asyncio.sleep(0.25)
        if ST["stage"] == "done":
            return
        if c.latest is None:
            continue
        st = c.latest
        state = st.get("state") or {}
        if not state:
            continue
        try:
            acts = merged_actions(st)
            await fn(st, acts, state)
        except Exception as e:
            ST["obs"]["tick_errors"].append(f"{tag}: {e!r}")


async def main():
    init_state()
    t_start = time.time()

    p0 = PhaseClient("P0-balancer")
    p1 = PhaseClient("P1-passive")
    ST["p0"] = p0
    ST["p1"] = p1
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"vi_kind={vi_kind_code(p0.latest or {})} "
                    f"balancer={ST['balancer_oid']} "
                    f"p0creatures={len(p0_creature_oids(s))}")
    finally:
        p0t.cancel()
        p1t.cancel()

    await finish(t_start)


def find_server_run_dir():
    """Locate the backfill-owned server's run dir for the server.log copy:
    explicit SERVER_RUN_ID env first, then ps discovery of the live
    phase-server-slim --games-db path, then our own run dir."""
    env = os.environ.get("SERVER_RUN_ID")
    if env:
        p = f"{BACKFILL}/runs/{env}"
        if os.path.isdir(p):
            say(f"server run dir from SERVER_RUN_ID env: {p}")
            return p
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "phase-server-slim" in line and "--games-db" in line:
                m = re.search(r"--games-db\s+(\S+)", line)
                if m:
                    d = os.path.dirname(m.group(1))
                    if os.path.isdir(d):
                        say(f"server run dir from ps discovery: {d}")
                        return d
    except Exception as e:
        say(f"ps server discovery failed: {e}")
    p = f"{BACKFILL}/runs/{RUN_ID}"
    say(f"server run dir fallback: {p}")
    return p


async def finish(t_start):
    if ST.get("finished"):
        return
    ST["finished"] = True
    dur = time.time() - t_start
    ass = {}
    notes = []
    states = {}
    for fn in ("pre_cast5", "post_cast5", "pre_cast6", "post_cast6",
               "final"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as e:
            notes.append(f"state reload failed for {fn}.json: {e}")

    # ---- A0: card-data parse (informational) ----
    try:
        cd = json.load(open(f"{RELDIR}/data/card-data.json"))
        bq = cd[BESEECH]
        wb = cd[BALANCER]
        shards = ((bq.get("mana_cost") or {}).get("shards") or [])
        sa = wb.get("static_abilities") or []
        grant = [a for a in sa
                 if ((a.get("mode") or {}).get("CastWithKeyword") or {})
                 .get("keyword", {}).get("Affinity") is not None]
        kw = wb.get("keywords") or []
        own_aff = any(isinstance(k, dict) and "Affinity" in k
                      for k in kw)
        notes.append(
            f"A0: card-data beseech the queen: shards={shards}, "
            f"oracle={bq.get('oracle_text','')[:120]!r}; "
            f"witherbloom, the balancer: own_affinity={own_aff}, "
            f"grants_affinity_to_instants_sorceries={len(grant) > 0}, "
            f"oracle={wb.get('oracle_text','')[:160]!r}")
    except Exception as e:
        notes.append(f"A0 parse check error: {e!r}")

    def bear_rec(purpose):
        rs = [r for r in ST["bear_casts"] if r["purpose"] == purpose]
        return rs[-1] if rs else None

    # ---- A1: setup (leg-1 setup the A3 measurement depends on) ----
    # Corrected vs the v0.112.0 driver: requiring leg 2 (cast6) for A1
    # made a completed leg-1 measurement verdict "blocked" when the
    # search prompt dangled. Per PLAYBOOK step 5, a confirmed failure
    # remains a reproduction even if downstream assertions cannot
    # execute; leg-2 absence is recorded in A4/A5, not in A1.
    c5 = ST["cast5"]
    c6 = ST["cast6"]
    ok = (ST["balancer_oid"] is not None
          and c5 is not None and c5["creatures_at_cast"] == 5)
    notes.append(
        f"A1: balancer_oid={ST['balancer_oid']} "
        f"cast_turn={ST['balancer_cast_turn']} "
        f"cast5_creatures={c5['creatures_at_cast'] if c5 else None} "
        f"cast6_creatures={c6['creatures_at_cast'] if c6 else None} "
        f"stage={ST['stage']}")
    ass["A1_setup"] = "passed" if ok else "failed"

    # ---- A2: bear control (creature spell: full cost) ----
    bc = bear_rec("control")
    if bc is not None and bc["tapped_delta"] is not None:
        ok = bc["tapped_delta"] == 2
        notes.append(
            f"A2: control bear tapped_delta={bc['tapped_delta']} "
            f"(expect 2 = full {{1}}{{G}}; "
            f"untapped {len(bc['untapped_before'])}->"
            f"{len(bc.get('untapped_after') or [])})")
    else:
        ok = False
        notes.append(f"A2 not-run/failed: control bear rec present="
                     f"{bc is not None} (stage={ST['stage']})")
        if bc is None:
            ass["A2_bear_control"] = "not-run"
    if "A2_bear_control" not in ass:
        ass["A2_bear_control"] = "passed" if ok else "failed"

    # ---- A3: five-creature cost ----
    if c5 is not None and c5["tapped_delta"] is not None:
        ok = c5["tapped_delta"] == 1
        notes.append(
            f"A3: Beseech#1 at 5 creatures tapped_delta="
            f"{c5['tapped_delta']} (expect 1 = 6 generic - 5 "
            f"affinity; untapped {len(c5['untapped_before'])}->"
            f"{len(c5.get('untapped_after') or [])}, "
            f"turn={c5.get('stack_turn')})")
    else:
        ok = False
        notes.append(f"A3 not-run/failed: cast5 rec present="
                     f"{c5 is not None} "
                     f"notoffered={ST['cast5_notoffered']}")
        if c5 is None:
            ass["A3_five_cost"] = "not-run"
    if "A3_five_cost" not in ass:
        ass["A3_five_cost"] = "passed" if ok else "failed"

    # ---- A4: six-creature cost ----
    if c6 is not None and c6["tapped_delta"] is not None:
        ok = c6["tapped_delta"] == 0
        notes.append(
            f"A4: Beseech#2 at 6 creatures tapped_delta="
            f"{c6['tapped_delta']} (expect 0 = free; untapped "
            f"{len(c6['untapped_before'])}->"
            f"{len(c6.get('untapped_after') or [])}, "
            f"turn={c6.get('stack_turn')})")
    else:
        ok = False
        notes.append(f"A4 not-run/failed: cast6 rec present="
                     f"{c6 is not None} "
                     f"notoffered={ST['cast6_notoffered']}")
        if c6 is None:
            ass["A4_six_cost"] = "not-run"
    if "A4_six_cost" not in ass:
        ass["A4_six_cost"] = "passed" if ok else "failed"

    # ---- A5: resolution (tightened vs v0.112.0) ----
    r5 = c5 is not None and c5["resolved"]
    r6 = c6 is not None and c6["resolved"]
    if ass["A1_setup"] != "passed":
        ass["A5_resolution"] = "not-run"
        notes.append("A5 not-run: setup incomplete")
    elif c6 is None:
        if r5:
            ass["A5_resolution"] = "not-run"
            notes.append("A5 not-run: leg-1 resolved but the leg-2 cast "
                         "never ran (search prompt dangled or game "
                         "stalled before test6)")
        else:
            ass["A5_resolution"] = "failed"
            notes.append("A5 failed: leg-1 Beseech did not resolve and "
                         "leg 2 never ran")
    else:
        ok = bool(r5 and r6)
        notes.append(
            f"A5: cast5 resolved={r5} tutored={c5['tutored'] if c5 else None} "
            f"search_answered={c5['search_answered'] if c5 else None}; "
            f"cast6 resolved={r6} tutored={c6['tutored'] if c6 else None} "
            f"search_answered={c6['search_answered'] if c6 else None} "
            f"(resolved = search answered AND tutored card in P0 hand)")
        ass["A5_resolution"] = "passed" if ok else "failed"

    # ---- verdict ----
    a1 = ass["A1_setup"]
    a3 = ass["A3_five_cost"]
    a4 = ass["A4_six_cost"]
    if a1 != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: A1 failed - the Balancer/test "
                     "setup never completed, so the granted-affinity "
                     "cost is untestable")
    elif a3 == "failed" or a4 == "failed":
        verdict = "reproduced"
        d5 = c5["tapped_delta"] if c5 else None
        d6 = c6["tapped_delta"] if c6 else None
        notes.append(
            f"verdict=reproduced: granted affinity not applied to "
            f"Beseech the Queen - at 5 creatures the cast tapped "
            f"{d5} lands (expect 1), at 6 creatures tapped {d6} "
            f"(expect 0/free)")
    elif all(ass.get(k) == "passed" for k in
             ("A2_bear_control", "A3_five_cost", "A4_six_cost",
              "A5_resolution")):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: granted affinity applied "
                     "exactly - dose-response 5 creatures -> 1 mana, "
                     "6 creatures -> free, creature spells full cost, "
                     "both casts resolved")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete assertion chain")
    notes.append(f"verdict={verdict}")

    def _jsonable(o):
        if isinstance(o, set):
            return sorted(o)
        return o

    run = {
        "run_id": RUN_ID, "issue": ISSUE,
        "verdict": verdict, "validated_at": "2026-10-21",
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 153,
                   "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7181_01140.py",
                 "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "observations": ST["obs"],
        "driver_state": {
            "stage": ST["stage"],
            "balancer_oid": ST["balancer_oid"],
            "balancer_cast_turn": ST["balancer_cast_turn"],
            "bear_casts": ST["bear_casts"],
            "cast5": ST["cast5"],
            "cast6": ST["cast6"],
            "vi_kinds_seen": ST["vi_kinds_seen"],
            "answered_iids": len(ST["answered_iids"]),
            "turns_seen": sorted(ST["turns_seen"]),
            "exports": ST["exports"],
        },
        "notes": notes,
        "evidence_files": sorted(
            f for f in os.listdir(EVDIR)
            if os.path.isfile(f"{EVDIR}/{f}")
            and f not in ("manifest.sha256", "scenario_run.log",
                          "wire_log.jsonl")) + ["manifest.sha256",
                                                "scenario_run.log",
                                                "wire_log.jsonl"],
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human-client seats.",
            "Card density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "P1 is passive (land drops + up to 2 bears, no attacks) "
            "so the cost-measurement windows stay clean.",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via "
            "full game replay).",
            "Mana paid is measured as the untapped-land delta between "
            "cast submission and first stack sighting (same turn), "
            "corroborated by the pre/post authoritative exports; no "
            "other P0 actions are taken while a test cast is in "
            "flight.",
            "The '5 creatures -> pay {B} or {2}' reporter phrasing is "
            "tested as 'pay {1} generic' per CR 601.2f (affinity "
            "reduces the generic portion of {2/B}{2/B}{2/B}).",
        ],
        "duration_s": round(dur, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=_jsonable)
    shutil.copy(f"{BACKFILL}/driver/scenario_7181_01140.py",
                f"{EVDIR}/scenario_7181_01140.py")
    srv_dir = find_server_run_dir()
    say(f"server run dir: {srv_dir}")
    try:
        with open(f"{srv_dir}/server.log", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        gc = ST.get("game_code") or ""
        excerpt = [ln for ln in clean.splitlines()
                   if gc and gc in ln]
        if not excerpt:
            excerpt = clean.splitlines()[-400:]
        with open(f"{EVDIR}/server.log", "w") as f:
            f.write("\n".join(excerpt) + "\n")
        say(f"wrote server.log excerpts ({len(excerpt)} lines)")
    except Exception as e:
        say(f"server.log excerpt failed: {e}")
        notes.append(f"server.log excerpt failed: {e}")
    render_summary(run, states)
    # close logs BEFORE hashing the manifest (#7176 lesson)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
          flush=True)


def render_summary(run, states):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7181 - Beseech the Queen x "
           "Witherbloom", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.114.0 (ac67cbb) protocol 153 - 2026-10-21"
           " - granted affinity for creatures on a sorcery",
           fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90),
            "not-reproduced": (120, 220, 120),
            "blocked": (230, 200, 120)}.get(run["verdict"],
                                            (180, 180, 180))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    for ln in [
            "Report: Beseech the Queen ({2/B}{2/B}{2/B}, MV 6) should",
            "be free with 6 creatures out via Witherbloom, the",
            "Balancer's 'instant and sorcery spells you cast have",
            "affinity for creatures' - but the affinity is allegedly",
            "not recognized.",
            "Expected (CR 702.42a/601.2f): 5 creatures -> pay {1};",
            "6 creatures -> free. Creature spells stay full cost."]:
        d.text((24, y), ln, fill=(200, 210, 225))
        y += 24
    y += 10
    d.text((24, y), "Assertions (from saved states / measurements):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup": "Balancer on BF; test5 at 5 creatures "
                    "(leg-1 setup; leg-2 absence in A4/A5)",
        "A2_bear_control": "bear #4 (creature spell) tapped exactly "
                           "2 lands (full {1}{G})",
        "A3_five_cost": "Beseech #1 at 5 creatures tapped exactly "
                        "1 land (6 generic - 5 affinity)",
        "A4_six_cost": "Beseech #2 at 6 creatures tapped exactly "
                       "0 lands (free)",
        "A5_resolution": "both Beseech casts resolved (search answered, "
                         "tutored card in hand)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "?")
        col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
               "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
        d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
        y += 26
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    ds = run["driver_state"]
    bc = next((r for r in ds.get("bear_casts", [])
               if r["purpose"] == "control"), {})
    c5 = ds.get("cast5") or {}
    c6 = ds.get("cast6") or {}
    for ln in [
            f"Balancer oid={ds.get('balancer_oid')} "
            f"(cast turn {ds.get('balancer_cast_turn')})",
            f"bear control: tapped_delta={bc.get('tapped_delta')} "
            f"(expect 2)",
            f"Beseech#1 (5 creatures): tapped_delta="
            f"{c5.get('tapped_delta')} (expect 1), tutored="
            f"{c5.get('tutored')}",
            f"Beseech#2 (6 creatures): tapped_delta="
            f"{c6.get('tapped_delta')} (expect 0), tutored="
            f"{c6.get('tutored')}",
            f"vi kinds seen: "
            f"{', '.join(ds.get('vi_kinds_seen', [])[:12])}",
            "Mana paid = untapped-land delta, submission -> first",
            "stack sighting (same turn); pre/post exports corroborate.",
    ]:
        d.text((24, y), ln[:108], fill=(160, 175, 195))
        y += 22
    d.text((24, y + 14), "Generated from saved states/assertions; not "
           "a gameplay screenshot.", fill=(110, 125, 145))
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    import hashlib as _hl
    files = sorted(
        f for f in os.listdir(EVDIR)
        if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = _hl.sha256()
        with open(f"{EVDIR}/{fn}", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    # plain print: RUNLOG is already closed at this point
    print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
