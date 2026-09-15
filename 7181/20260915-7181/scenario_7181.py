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
  v0.84.0 before the run; full Oracle text recorded in notes/A0.)

Oracle text (pinned card-data.json v0.84.0, verified 2026-09-15 before run):
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
  A1_setup          Balancer cast and on P0 BF for both tests; test5 ran
                    at exactly 5 P0 creatures, test6 at exactly 6.
  A2_bear_control   bear #4 (creature spell, Balancer on BF) tapped
                    exactly 2 lands (full {1}{G}; granted affinity is
                    sorcery/instant-only).
  A3_five_cost      Beseech #1 at 5 creatures tapped exactly 1 land
                    (6 generic - 5 affinity).
  A4_six_cost       Beseech #2 at 6 creatures tapped exactly 0 lands
                    (free).
  A5_resolution     both Beseech casts resolved: search prompt answered,
                    Beseech in P0 graveyard, tutored card recorded, no
                    Beseech stuck on stack.

Verdict rule:
  blocked        iff A1 fails (setup never reached).
  reproduced     iff A1 passes and (A3 fails or A4 fails): the granted
                 affinity did not reduce Beseech's cost as reported
                 (tapped more - or a different wrong amount - than the
                 affinity math allows). A5 may be not-run then.
  not-reproduced iff A2, A3, A4, A5 all pass: the granted affinity is
                 applied exactly (dose-response 5->1, 6->0, bears full).

Evidence: evidence/7181/<run-id>/pre_cast5.json, cast5_detail.json,
post_cast5.json, pre_cast6.json, cast6_detail.json, post_cast6.json,
final.json, run.json, manifest.sha256, summary.png, scenario_7181.py,
wire_log.jsonl, scenario_run.log, server.log (excerpts).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7181
RUN_ID = os.environ.get("RUN_ID", "20260915-7181")
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

RELDIR = f"{BACKFILL}/server/releases/v0.84.0"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
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
                      "2026-09-15; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run; "
                      "release v0.84.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-15",
    "observed_at": "2026-09-15",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9375",
    "source": "verified pin; isolated server on 127.0.0.1:9375 started by "
              "this run under runs/20260915-7181/",
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


def ref_key(ref):
    """Normalize a candidate reference to a comparable string oid."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


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


def gy_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("owner") == pid
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
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


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


def build_submission(iid, rtype, choice_id=None, empty_sequence=False):
    if rtype == "schema":
        data = {"choiceIds": []} if empty_sequence else {
            "choiceIds": [choice_id]}
        return {"interactionId": iid,
                "response": {"type": "sequence", "data": data}}
    return {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


def drain_rejections(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            out.append({"type": t, "data": data})
    return out


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> bear_control_cast -> test5 ->
                            # test5_cast -> test5_resolving -> test6setup ->
                            # test6 -> test6_cast -> test6_resolving ->
                            # final -> done
        "game_code": None,
        "balancer_oid": None,
        "balancer_cast": False,
        "balancer_cast_turn": None,
        # bear casts: list of {purpose, oid, untapped_before, tapped_delta,
        #                        stack_seen, resolved}
        "bear_casts": [],
        # test casts: {"submitted","untapped_before","stack_seen",
        #              "tapped_delta","search_answered","tutored","resolved"}
        "cast5": None,
        "cast6": None,
        "cast5_detail_saved": False,
        "cast6_detail_saved": False,
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "final_exported": False,
        "exports": {},
        "last_rev_acted": {},
        "notes_extra": [],
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-balancer")
    p1 = PhaseClient("P1-passive")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
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
            obs["tick_errors"].append(f"export_{name}: {e!r}")
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
                "waiting_for": state.get("waiting_for"),
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
            obs["tick_errors"].append(f"detail_{name}: {e!r}")

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def mulligan_pending_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = (p.get("phase") or {})
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan_keep(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if any(k in tx for k in protect):
                    return 2
                if FOREST in tx or SWAMP in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                build_submission(iid, resp.get("type"), pick.get("id")))
            return True
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

    async def answer_search(c, pid, tag, st, state):
        """Answer Beseech the Queen's library-search prompt with the
        first legal candidate. Returns True if a submission was made."""
        wtype = (wf_of(state).get("type") or "")
        if "search" not in wtype.lower():
            return False
        wp = wf_player(state)
        if wp is not None and wp != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response") or {}
            data = resp.get("data") or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            pick = chs[0]
            ref = ref_key([(s.get("data") or {}).get("reference")
                           for s in (pick.get("surfaces") or [])])
            nm = (lname(state, int(ref)) if ref
                  and str(ref).lstrip("-").isdigit() else pick.get("id"))
            sub = build_submission(iid, resp.get("type"), pick.get("id"))
            wire("search_answer",
                 {"tag": tag, "wtype": wtype, "choice": nm,
                  "submission": sub})
            await c.send_interaction(sub)
            ST["answered_iids"].append(iid)
            for key in ("cast5", "cast6"):
                rec = ST[key]
                if rec is not None and not rec.get("search_answered"):
                    rec["search_answered"] = True
                    rec["tutored"] = nm
            say(f"[{tag}] search prompt answered: tutored '{nm}'")
            return True
        return False

    async def combat_empty(c, pid, tag, st, state, acts):
        rev = st.get("state_revision", -1)
        wtype = wf_of(state).get("type") or ""
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_{wtype}", rev):
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                else:
                    sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
        return False

    CAST_QUIET = ("bear_control_cast", "test5_cast", "test5_resolving",
                  "sixth_cast", "test6_cast", "test6_resolving", "final")

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state,
                                (BALANCER, BESEECH)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        for rj in drain_rejections(p0):
            obs["rejections"].append({"stage": ST["stage"], **rj})
            say(f"[P0] REJECTION in stage {ST['stage']}: "
                f"{json.dumps(rj, default=str)[:300]}")
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
                    if await export_named(post):
                        pass
                    ST["stage"] = next_stage
                    return
            elif not rec["resolved"]:
                want_gy = 1 if key == "cast5" else 2
                if len(gy_ids(state, 0, BESEECH)) >= want_gy \
                        and rec["search_answered"]:
                    rec["resolved"] = True
                    say(f"[P0] {key}: Beseech RESOLVED, tutored "
                        f"'{rec['tutored']}'")
                    ST["stage"] = ("test6setup" if key == "cast5"
                                   else "final")
                    return

        if ST["stage"] == "final" and not ST["final_exported"]:
            if await export_named("final"):
                ST["final_exported"] = True
                ST["stage"] = "done"
                say("[P0] final exported; stage -> done")
            return

        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # ---- main-phase actions (never during a cast flight) ----
        if ST["stage"] not in CAST_QUIET and my_priority(state, 0) \
                and phase in ("PreCombatMain", "PostCombatMain") \
                and not stack_entries(state):
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
                    return
            # bears: ramp (3) -> control (#4) -> sixth (#5)
            nbears = len(bf_ids(state, 0, BEAR))
            purpose = None
            if ST["balancer_oid"] is None:
                if nbears < 3:
                    purpose = "ramp"
            elif ST["stage"] == "setup" and nbears == 3:
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
                    if not ST.get("cast5_notoffered"):
                        ST["cast5_notoffered"] = True
                        save_prompt_detail("cast5", state, st)
                        obs["unexpected_prompts"].append(
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
                    ST["stage"] = "test5_cast"
                    return
            # TEST6: Beseech #2 at exactly 6 creatures
            if ST["stage"] == "test6" \
                    and len(p0_creature_oids(state)) == 6 \
                    and BESEECH in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, 0, BESEECH)
                if ca is None:
                    if not ST.get("cast6_notoffered"):
                        ST["cast6_notoffered"] = True
                        save_prompt_detail("cast6", state, st)
                        obs["unexpected_prompts"].append(
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
                    ST["stage"] = "test6_cast"
                    return

        # default: pass priority (always fall through, cf. #6862)
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state, ()):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
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
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

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
                obs["tick_errors"].append(f"{tag}: {e!r}")

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
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"balancer={ST['balancer_oid']} "
                    f"p0creatures={len(p0_creature_oids(s))}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
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

        # ---- A1: setup ----
        c5 = ST["cast5"]
        c6 = ST["cast6"]
        ok = (ST["balancer_oid"] is not None
              and c5 is not None and c5["creatures_at_cast"] == 5
              and c6 is not None and c6["creatures_at_cast"] == 6)
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
            notes.append(f"A2 not-run/failed: control bear rec={bc} "
                         f"(stage={ST['stage']})")
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
                         f"notoffered={ST.get('cast5_notoffered')}")
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
                         f"notoffered={ST.get('cast6_notoffered')}")
            if c6 is None:
                ass["A4_six_cost"] = "not-run"
        if "A4_six_cost" not in ass:
            ass["A4_six_cost"] = "passed" if ok else "failed"

        # ---- A5: resolution ----
        r5 = c5 is not None and c5["resolved"]
        r6 = c6 is not None and c6["resolved"]
        if (c5 is None or c6 is None) and ass["A1_setup"] != "passed":
            ass["A5_resolution"] = "not-run"
            notes.append("A5 not-run: setup incomplete")
        else:
            ok = bool(r5 and r6)
            notes.append(
                f"A5: cast5 resolved={r5} tutored={c5['tutored'] if c5 else None} "
                f"search_answered={c5['search_answered'] if c5 else None}; "
                f"cast6 resolved={r6} tutored={c6['tutored'] if c6 else None} "
                f"search_answered={c6['search_answered'] if c6 else None}")
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
        for n in ST["notes_extra"]:
            notes.append(n)

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 71,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {
                "stage": ST["stage"],
                "balancer_oid": ST["balancer_oid"],
                "balancer_cast_turn": ST["balancer_cast_turn"],
                "bear_casts": ST["bear_casts"],
                "cast5": ST["cast5"],
                "cast6": ST["cast6"],
                "wf_types_seen": ST["wf_types_seen"],
                "turns_seen": sorted(ST["turns_seen"]),
                "exports": ST["exports"],
            },
            "notes": notes,
            "evidence_files": ["pre_cast5.json", "cast5_detail.json",
                               "post_cast5.json", "pre_cast6.json",
                               "cast6_detail.json", "post_cast6.json",
                               "final.json", "run.json", "manifest.sha256",
                               "summary.png", f"scenario_{ISSUE}.py",
                               "wire_log.jsonl", "scenario_run.log",
                               "server.log"],
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
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        say(f"server run dir: runs/{srv_run}")
        try:
            with open(f"{BACKFILL}/runs/{srv_run}/server.log", "rb") as f:
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
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
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
            "A1_setup": "Balancer on BF; test5 at 5 creatures, "
                        "test6 at 6 creatures",
            "A2_bear_control": "bear #4 (creature spell) tapped exactly "
                               "2 lands (full {1}{G})",
            "A3_five_cost": "Beseech #1 at 5 creatures tapped exactly "
                            "1 land (6 generic - 5 affinity)",
            "A4_six_cost": "Beseech #2 at 6 creatures tapped exactly "
                           "0 lands (free)",
            "A5_resolution": "both Beseech casts resolved via the "
                             "library-search prompt",
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
                f"wf types seen: "
                f"{', '.join(ds.get('wf_types_seen', [])[:12])}",
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

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
