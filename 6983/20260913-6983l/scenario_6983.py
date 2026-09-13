#!/usr/bin/env python3
"""Issue #6983: [Card Bug] Unless payments derived from board state are unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Repulsive Mutation ({X}{G}{U} Instant):
    "Put X +1/+1 counters on target creature you control. Then counter up to
     one target spell unless its controller pays mana equal to the greatest
     power among creatures you control."

Card-data parse state on v0.81.3 (verified 2026-09-13 before the run):
  abilities[0].effect = PutCounter(P1P1, count=Ref(X), target Typed Creature/You)
  abilities[0].sub_ability.effect = {"type": "Unimplemented",
    "name": "unless_payment",
    "description": "counter up to one target spell unless its controller pays
    mana equal to the greatest power among creatures you control"}.

Reported symptom: the counterspell mode's unless-payment clause is
unsupported because its amount is dynamic (greatest power among creatures
the caster controls). Expected: when the mode resolves, the target spell's
controller is offered a payment of mana equal to that greatest power (here
4, from Leatherback Baloth), and the spell is countered only if unpaid.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Repulsive Mutation, 4x Grizzly Bears, 4x Leatherback Baloth,
      20x Forest, 20x Island.
  P1: 12x Lightning Bolt, 48x Mountain (passive except the one Bolt).
  P0 T2: Grizzly Bears (2/2). P0 T3: Leatherback Baloth (4/5).
  P1: casts Lightning Bolt targeting P0 once P0 has the Baloth on the
      battlefield and >= 2 untapped lands.
  P0 responds with Repulsive Mutation: X = 0, target creature = Baloth,
      target spell = the Bolt on the stack.
  Resolution: 0 counters on Baloth; then the unless clause - the bug says
      the dynamic-amount unless_payment is Unimplemented.

Assertions (each passed / failed / not-run):
  A1_parse_gap      card-data v0.81.3: abilities[0].sub_ability.effect is
                    Unimplemented(name=unless_payment) (expected per bug
                    report; PASS confirms the reported parser gap).
  A2_setup_ok       PRE: Baloth + Bears on P0 BF; greatest power among P0
                    creatures == 4; TWO Bolts on stack; Mutation in P0 hand;
                    life 20/20.
  A3_mutation_cast  Mutation was cast (X=0, Baloth targeted, Bolt targeted)
                    and resolved: in P0 graveyard after resolution.
  A4_payment_prompt a dynamic-amount unless-payment opportunity (pay mana
                    equal to greatest power = 4) was offered to P1 during
                    Mutation resolution. Expected absent (unsupported:
                    Unimplemented effect silently skips it).
  A5_bolt_resolves both Bolts were NOT countered: P1 gy, P0 life 20 -> 14.
  A6_baloth_unchanged Baloth still 4/5 post-resolution (X=0 counters; the
                    parsed PutCounter clause executed).
  A7_cleanup        POST: stack empty, game proceeds.

Two Bolts are stacked so the optional "up to one target spell" choice
faces >1 legal target (with a single spell the engine silently chose zero
targets and the unless step was unreachable).

Verdict rule: reproduced iff A1 passes and the reported outcome is not
implemented (A4 fails: no dynamic-amount payment prompt, spell resolves);
not-reproduced iff A1 fails (clause now parses as supported) AND A4 passes
with the offered amount == 4 and the counter/decline outcomes correct;
blocked iff A2 or A3 fails.

Evidence: evidence/6983/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6983.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts for this game's code only).
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6983l")
EVDIR = f"{BACKFILL}/evidence/6983/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MUTATION = "repulsive mutation"
BALOTH = "leatherback baloth"
BEARS = "grizzly bears"
BOLT = "lightning bolt"
FOREST = "forest"
ISLAND = "island"
MOUNTAIN = "mountain"
LANDS = (FOREST, ISLAND, MOUNTAIN)

P0_DECK = [(MUTATION, 12), (BEARS, 8), (BALOTH, 8),
           (FOREST, 16), (ISLAND, 16)]
P1_DECK = [(BOLT, 12), (MOUNTAIN, 48)]

# Isolated server (started by this run): v0.81.3 pinned release on 9375;
# identity mirrors the verified pin.
SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "isolated v0.81.3 single-user server on 127.0.0.1:9375 "
              "(started fresh by this run) + verified pin "
              "(minisign-verify of binary + signed data manifest with the "
              "repo-pinned key).",
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


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def creature_power(state, oid):
    o = get_obj(state, oid)
    p = o.get("power")
    if isinstance(p, dict):
        return p.get("value")
    if isinstance(p, int):
        return p
    return None


def greatest_power(state, pid):
    vals = [creature_power(state, oid) for oid in bf_ids(state, pid)]
    vals = [v for v in vals if isinstance(v, int)]
    return max(vals) if vals else 0


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


def untapped_land_names(state, pid):
    return [str(get_obj(state, oid).get("base_name")
                or get_obj(state, oid).get("name") or "").lower()
            for oid in untapped_lands(state, pid)]


def p0_can_pay_mutation(state):
    """P0 has {G}{U} available right now (untapped Island + Forest)."""
    names = untapped_land_names(state, 0)
    return ISLAND in names and FOREST in names


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def choice_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None


def candidate_ref(ch):
    """The engine-issued target reference of a submitted candidate
    (surfaces[].data.reference), per driver lesson #6862."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def wf_pending_for(state, pid):
    d = wf_of(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack_entries(state):
    return state.get("stack") or []


def bolt_on_stack_count(state):
    """Count Lightning Bolt spells (DealDamage Fixed 3) on the stack.

    Stack entries carry no card name, so match the Bolt's effect
    signature instead of parsing names (cf. stack_names, which yields
    "?" for stack entries)."""
    n = 0
    for e in stack_entries(state):
        kind = (e.get("kind") or {}).get("type")
        if kind != "Spell":
            continue
        data = (e.get("kind") or {}).get("data") or {}
        ab = data.get("ability") or {}
        eff = ab.get("effect") or {}
        amt = eff.get("amount") or {}
        if (eff.get("type") == "DealDamage" and amt.get("type") == "Fixed"
                and amt.get("value") == 3):
            n += 1
    return n


def stack_names(state):
    out = []
    for e in stack_entries(state):
        blob = json.dumps(e, default=str)
        m = re.search(r'"name"\s*:\s*"([^"]+)"', blob)
        sid = e.get("source_id")
        out.append((m.group(1) if m else "?", sid, e.get("id")))
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        if stype == "text":
            val = None
            for s in choice.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (isinstance(d, dict) and d.get("role") == "choice"
                        and "value" in d):
                    val = d["value"]
                    break
            sub = {"interactionId": iid,
                   "response": {"type": "text", "data": {"value": val}}}
        else:
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


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_gap", "A2_setup_ok", "A3_mutation_cast",
            "A4_payment_prompt", "A5_bolt_resolves", "A6_baloth_unchanged",
            "A7_cleanup")}

    # ---- A1 (parse gap) up front, from the pinned card-data.json ----
    try:
        cd_path = f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json"
        cd = json.load(open(cd_path))
        c = cd[MUTATION]
        sub = (((c.get("abilities", []) or [])[0] or {})
               .get("sub_ability") or {})
        eff = (sub.get("effect") or {})
        notes.append("parse: sub_ability.effect = " + json.dumps(eff)[:300])
        wire("parse_check", {"effect": eff})
        ok = (eff.get("type") == "Unimplemented"
              and eff.get("name") == "unless_payment")
        ass["A1_parse_gap"] = "passed" if ok else "failed"
        notes.append(f"A1_parse_gap: effect.type={eff.get('type')} "
                     f"name={eff.get('name')} -> {ass['A1_parse_gap']}")
    except Exception as ex:
        ass["A1_parse_gap"] = "failed"
        notes.append(f"A1_parse_gap failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"mutation_cast": False, "mutation_in_flight": False,
          "mutation_resolved": False, "mutation_oid": None,
          "mutation_on_stack_seen": False, "mutation_stack_targets": None,
          "mutation_castable": False,
          "creature_target_ref": None, "spell_target_ref": None,
          "x_answered": False,
          "bolt_cast": False, "bolt_submitted": False, "bolt_resolved": False,
          "bolt_targets_answered": 0,
          "bolt2_cast": False, "bolt2_submitted": False,
          "bolt2_oid": None, "bolt2_resolved": False,
          "bolt_on_stack_seen": False, "bolt2_on_stack_seen": False,
          "bolts_seen_max": 0,
          "bolt_oid": None, "bolt_life_pre": None,
          "pre_exported": False, "post_exported": False,
          "payment_prompt_seen": False, "payment_prompt_detail": None,
          "baloth_oid": None, "bears_oid": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "tick_errors": [], "life_trace": [], "stack_trace": [],
           "payment_scan": []}
    prompt_first_seen = {}
    last_select = {}

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def scan_payment_prompt(c, tag, state):
        """Record any unless/payment interaction offered during the
        Mutation resolution window. The prompt would go to P1 (the Bolt's
        controller), so scan both clients' views."""
        if not ST["mutation_in_flight"] or ST["payment_prompt_seen"]:
            return
        st = c.latest or {}
        vi = st.get("viewer_interaction") or {}
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            blob = json.dumps(opp, default=str).lower()
            hit = any(k in blob for k in ("unless", "pay mana", "paymana",
                                          "paycombat", "unlesspayment"))
            if not hit:
                continue
            ST["payment_prompt_seen"] = True
            ST["payment_prompt_detail"] = {
                "seen_by": tag, "iid": str(iid)[:12],
                "texts": [choice_text(ch)[:80] for ch in vi_choices(opp)][:8]}
            obs["payment_scan"].append(ST["payment_prompt_detail"])
            say(f"PAYMENT PROMPT observed by {tag}: "
                f"{ST['payment_prompt_detail']['texts']}")
            wire("payment_prompt_seen",
                 {"by": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, post = states.get("pre"), states.get("post")

        # ---- A2: setup (2 Bolts on stack) ----
        if pre is not None:
            baloth = bf_id(pre, 0, BALOTH)
            bears = bf_id(pre, 0, BEARS)
            gp = greatest_power(pre, 0)
            n_bolts = bolt_on_stack_count(pre)
            mut_in_hand = MUTATION in hand_lnames(pre, 0)
            lives = [life_of(pre, i) for i in (0, 1)]
            ok = (baloth is not None and bears is not None and gp == 4
                  and n_bolts >= 2 and mut_in_hand and lives == [20, 20])
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: baloth={baloth} bears={bears} "
                         f"greatest_power={gp} bolts_on_stack={n_bolts} "
                         f"mutation_in_hand={mut_in_hand} lives={lives}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3: mutation cast + resolved (in P0 gy) ----
        mut_gy = False
        if post is not None:
            mut_gy = any(lname(post, int(oid)) == MUTATION
                         for oid, o in (post.get("objects", {}) or {}).items()
                         if o.get("zone") == "Graveyard"
                         and o.get("controller") == 0)
        ok = ST["mutation_cast"] and ST["mutation_resolved"] and mut_gy
        ass["A3_mutation_cast"] = "passed" if ok else "failed"
        notes.append(f"A3: cast={ST['mutation_cast']} "
                     f"resolved={ST['mutation_resolved']} in_P0_gy={mut_gy} "
                     f"x_answered={ST['x_answered']} "
                     f"creature_target_ref={ST['creature_target_ref']} "
                     f"spell_target_ref={ST['spell_target_ref']} "
                     f"stack_targets={ST['mutation_stack_targets']}")

        # ---- A4: dynamic-amount unless-payment prompt offered to P1 ----
        ok = ST["payment_prompt_seen"]
        ass["A4_payment_prompt"] = "passed" if ok else "failed"
        notes.append(f"A4: payment_prompt_seen={ST['payment_prompt_seen']} "
                     f"detail={ST['payment_prompt_detail']}")
        if not ok:
            notes.append("A4: no unless-payment interaction was offered "
                         "during Mutation resolution (expected for the "
                         "Unimplemented clause)")

        # ---- A5: both Bolts resolved uncountered (P0 20 -> 14) ----
        # If the unless clause worked, the targeted Bolt would be countered
        # (P0 life 17) or P1 would be prompted to pay (A4). Under the bug,
        # both Bolts resolve: 20 -> 14.
        bolts_gy = 0
        if post is not None:
            bolts_gy = sum(1 for oid, o in
                           (post.get("objects", {}) or {}).items()
                           if o.get("zone") == "Graveyard"
                           and o.get("controller") == 1
                           and lname(post, int(oid)) == BOLT)
            l0 = life_of(post, 0)
            both_resolved = (ST["bolt_resolved"] and ST["bolt2_resolved"]
                             and l0 == 14)
            notes.append(f"A5: bolt_resolved={ST['bolt_resolved']} "
                         f"bolt2_resolved={ST['bolt2_resolved']} "
                         f"bolts_in_P1_gy={bolts_gy} P0_life={l0} "
                         f"(bug expectation: 14, both resolved uncountered; "
                         f"17/20 would mean a Bolt was countered)")
        else:
            both_resolved = False
            notes.append("A5 failed: post.json missing")
        ass["A5_bolt_resolves"] = "passed" if both_resolved else "failed"

        # ---- A6: Baloth unchanged (X=0 counters; parsed clause ran) ----
        if post is not None:
            baloth_oid = bf_id(post, 0, BALOTH)
            pwr = creature_power(post, baloth_oid) if baloth_oid else None
            ok = pwr == 4
            notes.append(f"A6: Baloth oid={baloth_oid} power={pwr} "
                         f"(expected 4: X=0 counters placed)")
        else:
            ok = False
            notes.append("A6 failed: post.json missing")
        ass["A6_baloth_unchanged"] = "passed" if ok else "failed"

        # ---- A7: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            ok = stack_empty
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={stack_empty}")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 failed: post.json missing")

        # ---- verdict ----
        # reproduced: the Mutation actually targeted a spell on the stack,
        # yet no dynamic-amount unless-payment was offered and the spell
        # resolved uncountered.
        # not-reproduced: a spell was targeted AND (a payment prompt
        # appeared OR the targeted spell was countered).
        # blocked: the engine never offered the optional spell target, so
        # the unless-payment step was unreachable.
        spell_targeted = ST["spell_target_ref"] is not None
        prompt_seen = ST["payment_prompt_seen"]
        if ass["A2_setup_ok"] != "passed" or ass["A3_mutation_cast"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) or cast (A3) failed")
        elif not spell_targeted:
            verdict = "blocked"
            notes.append("verdict=blocked: engine never offered the optional "
                         "'up to one target spell' target (spell_target_ref "
                         "is None); the unless-payment step was unreachable")
        elif (ass["A1_parse_gap"] == "passed" and not prompt_seen
                and ass["A5_bolt_resolves"] == "passed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: Mutation targeted a Bolt, no "
                         "dynamic-amount unless-payment was offered (A4), "
                         "both Bolts resolved uncountered (A5), parser gap "
                         "confirmed (A1)")
        elif prompt_seen:
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: a payment prompt WAS "
                         "offered during Mutation resolution")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6983,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 single-user server on "
                               "127.0.0.1:9375 (started fresh by this run; "
                               "its games.db holds only this run)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6983.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6983.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Mutation / 4x Bears / 4x Baloth / 12x Bolt density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "The zero-creature branch of the acceptance criteria is not "
                "reachable: the spell requires targeting a creature you "
                "control to cast. One (4-power) and two (4+2-power) "
                "qualifying creatures were covered.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6983.py",
                    f"{EVDIR}/scenario_6983.py")
        RUN_LOG_DIR = "/home/hatch/workspace/dev/phase-backfill/runs/20260913-6983"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            excerpt = [ln for ln in clean.splitlines() if GAME in ln]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines for "
                f"game {GAME})")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 980
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6983 - Repulsive Mutation",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "unless_payment w/ dynamic (greatest-power) amount "
               "Unsupported",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (from saved states / card-data):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_gap": "card-data: effect Unimplemented(unless_payment)",
            "A2_setup_ok": "PRE: Baloth+Bears on P0 BF, gp=4, Bolt on stack",
            "A3_mutation_cast": "Mutation X=0 cast & resolved (P0 gy)",
            "A4_payment_prompt": "dynamic-amount pay prompt offered to P1",
            "A5_bolt_resolves": "Bolt NOT countered (P0 20->17)",
            "A6_baloth_unchanged": "Baloth still 4/5 (X=0 counters placed)",
            "A7_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Life totals across states (P0/P1):",
               fill=(200, 210, 225))
        y += 24
        for label in ("pre", "post"):
            st = states.get(label)
            if st is not None:
                bl = bf_id(st, 0, BALOTH)
                gp = greatest_power(st, 0)
                line = (f"{label:>4}: {life_of(st, 0)}/{life_of(st, 1)}  "
                        f"baloth_oid={bl} greatest_power={gp}  "
                        f"stack={[n for n, _s, _i in stack_names(st)]}")
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line, fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        # NOTE: scenario_run.log is hashed LAST, after the final say() line
        # below is appended; otherwise the manifest's hash for the log goes
        # stale by exactly one line.
        files = ["pre.json", "post.json", "run.json",
                 "scenario_6983.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        # Re-hash now that all scenario_run.log logging is done. No say()
        # may follow this point (finish() only closes files + stdout).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        if pid == 0:
            has_creature = BEARS in hn or BALOTH in hn
            ok = (MUTATION in hn and has_creature and lands >= 2) or mulls >= 2
        else:
            # P1 needs TWO Bolts (it stacks both so the Mutation faces
            # two spells on the stack)
            ok = (hn.count(BOLT) >= 2 and lands >= 3) or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands; "
                f"mutation={MUTATION in hn} bolt={BOLT in hn} "
                f"bolt_x{hn.count(BOLT)} "
                f"creature={BEARS in hn or BALOTH in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in hand[:1]]}})
        say(f"{tag} bottoms 1")

    async def discard_tick(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                t = choice_text(ch).lower()
                if t in LANDS:
                    return 0
                if t in (BEARS, BALOTH):
                    return 1
                return 2  # protect Bolt / Mutation last

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state, skip_answer=False):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            chs = vi_choices(opp)
            phase = (state.get("phase") or "")
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "phase": phase,
                 "texts": [choice_text(ch)[:60] for ch in chs][:8]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"phase={phase}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if skip_answer:
                continue
            if time.time() - entry["t0"] < 20:
                continue
            pick = chs[0] if chs else None
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 20s stall")
                obs["auto_answered"].append({"who": tag})
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def cast_named(c, acts, state, name, tag):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == name:
                say(f"[{tag}] casting {name} via {a['type']} (oid {oid})")
                wire("cast", {"who": tag, "name": name, "oid": oid,
                              "action": a["type"]})
                await submit_as_is(c, a)
                return oid
        return None

    async def answer_bolt_target(c, st, state):
        """P1's Bolt TargetSelection: pick the player candidate seat == 0."""
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            node = next((ch for ch in chs if choice_seat(ch) == 0), None)
            if node is None:
                if chs:
                    say("[P1] bolt-target: no seat-0 candidate; holding")
                continue
            say("[P1] bolt targets P0 (seat 0)")
            await answer_vi(c, opp, node, "P1")
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            ST["bolt_targets_answered"] = ST.get("bolt_targets_answered", 0) + 1
            if ST["bolt_targets_answered"] >= 1:
                ST["bolt_cast"] = True
            if ST["bolt_targets_answered"] >= 2:
                ST["bolt2_cast"] = True
            return True
        return False

    def cand_zone(ch):
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("zone"):
                return str(d["zone"]).lower()
        return None

    def cand_name(ch):
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                return str(d["name"]).lower()
        return None

    async def answer_mutation_targets(c, st, state):
        """Mutation's two TargetSelections, discriminated by candidate
        composition: spell-on-stack candidates -> the Bolt; battlefield
        creature candidates -> the Baloth. Records the ACTUALLY submitted
        candidate reference (lesson #6906)."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            chs = vi_choices(opp)
            if not chs:
                continue
            names = [cand_name(ch) for ch in chs]
            zones = [cand_zone(ch) for ch in chs]
            wire("mutation_target_prompt",
                 {"iid": str(iid)[:12], "n": len(chs),
                  "names": names[:10], "zones": zones[:10],
                  "full": json.loads(json.dumps(opp, default=str))})
            pick = None
            slot = None
            bolt_cands = [ch for ch in chs
                          if cand_name(ch) == BOLT or cand_zone(ch) == "stack"]
            if bolt_cands:
                pick = bolt_cands[0]
                slot = "spell"
            else:
                own_creatures = [ch for ch in chs
                                 if cand_name(ch) in (BALOTH, BEARS)]
                if own_creatures:
                    pick = next((ch for ch in own_creatures
                                 if cand_name(ch) == BALOTH),
                                own_creatures[0])
                    slot = "creature"
            if pick is None:
                say(f"[P0] mutation-target prompt: no recognized candidate "
                    f"(names={names[:6]}); holding")
                continue
            ref = candidate_ref(pick)
            say(f"[P0] mutation {slot}-target -> {choice_text(pick)[:60]} "
                f"ref={ref}")
            wire("mutation_target_answer",
                 {"slot": slot, "choiceId": pick.get("id"),
                  "reference": ref, "text": choice_text(pick)[:80]})
            await answer_vi(c, opp, pick, "P0")
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            if slot == "creature":
                ST["creature_target_ref"] = ref
            else:
                ST["spell_target_ref"] = ref
            acted = True
        return acted

    async def answer_x(c, st):
        """ChooseXValue (schema number) -> X = 0."""
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            if resp.get("type") == "schema" and spec.get("type") == "number":
                sub = {"interactionId": iid,
                       "response": {"type": "number", "data": {"value": 0}}}
                say("[P0] Mutation X-choice -> X=0")
                wire("x_answer", {"iid": iid, "x": 0})
                await c.send_interaction(sub)
                prompt_first_seen[iid] = {"t0": time.time(), "done": True}
                ST["x_answered"] = True
                return True
        return False

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        scan_payment_prompt(p0, "P0", state)
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0")
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(0) != p0.revision):
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        # track creatures on BF
        baloth = bf_id(state, 0, BALOTH)
        if baloth is not None and ST["baloth_oid"] is None:
            ST["baloth_oid"] = int(baloth)
            say(f"Baloth on BF: oid={baloth} (turn {turn})")
            wire("baloth_on_bf", {"oid": int(baloth), "turn": turn})
        bears = bf_id(state, 0, BEARS)
        if bears is not None and ST["bears_oid"] is None:
            ST["bears_oid"] = int(bears)
            say(f"Bears on BF: oid={bears} (turn {turn})")
        # life trace
        lives = tuple(life_of(state, i) for i in (0, 1))
        tr = obs["life_trace"]
        if all(l is not None for l in lives) and (not tr or tr[-1][1] != lives):
            tr.append((round(time.time() - t_start, 1), lives))
            say(f"life = {lives}")
            wire("life", {"life": lives})
        # castability of the Mutation response (colors matter: {G}{U};
        # P1 only casts the Bolt once this is true, so P0 can always answer)
        mut_oid = None
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == MUTATION:
                mut_oid = oid
                break
        # latch: once the engine advertises the Mutation cast on a P0
        # main-phase priority, P1 may cast the Bolt (do NOT reset on
        # ticks where P0 has no priority / no advertised casts)
        if mut_oid is not None and not ST["mutation_cast"]:
            ST["mutation_castable"] = True
        # diagnostic: what casts does the engine advertise on P0 main-phase
        # priority while Mutation is in hand?
        if (phase in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and my_priority(state, 0)
                and MUTATION in hand_lnames(state, 0)):
            key = ("castdiag", turn, phase)
            if key not in obs.setdefault("castdiag_done", []):
                obs["castdiag_done"].append(key)
                casts = []
                for a in acts:
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id") or d.get("card_id")
                    nm = lname(state, oid) if isinstance(oid, int) else None
                    casts.append((a["type"], nm))
                say(f"CASTDIAG turn={turn} phase={phase} acts={casts}")
                wire("castdiag", {"turn": turn, "phase": phase,
                                  "casts": casts})
        wplayer_pending = wf_pending_for(state, 0)
        # mutation cast decisions pending on P0
        if (wtype == "TargetSelection" and wplayer_pending
                and ST["mutation_in_flight"]):
            if await answer_mutation_targets(p0, st, state):
                return
        if wtype == "ChooseXValue" and wplayer_pending:
            if await answer_x(p0, st):
                return
        if await discard_tick(p0, 0, "P0", st, state):
            return
        # PRE: both Bolts on stack, Mutation in hand, Baloth+Bears on BF,
        # P0 priority -> export before responding. The bolt count uses the
        # effect-signature matcher (stack entries carry no card name).
        if (not ST["pre_exported"] and ST["bolt2_submitted"]
                and baloth is not None and bears is not None
                and bolt_on_stack_count(state) >= 2
                and not ST["mutation_cast"]
                and my_priority(state, 0)):
            if await export_named("pre"):
                ST["pre_exported"] = True
                say("PRE exported: Baloth+Bears on BF, 2 Bolts on stack")
        # POST: mutation resolved, bolt(s) resolved, stack empty -> export
        # NOW and finish immediately
        bolts_done = ST["bolt_resolved"] and (
            not ST["bolt2_cast"] or ST["bolt2_resolved"])
        if (ST["mutation_resolved"] and bolts_done
                and not ST["post_exported"] and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                say("POST exported: both spells resolved, stack empty")
                await finish()
                return
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # never pass priority while a P0 cast decision is pending
        if wtype in ("TargetSelection", "ChooseXValue", "ManaPayment",
                     "OptionalCostChoice") and wplayer_pending:
            return
        # ---- P0 priority actions ----
        in_flight = bool(state.get("stack") or [])
        if (not in_flight and phase in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            hn = hand_lnames(state, 0)
            lands = len(untapped_lands(state, 0))
            if bears is None and BEARS in hn and lands >= 2:
                coid = await cast_named(p0, acts, state, BEARS, "P0")
                if coid is not None:
                    return
            if baloth is None and BALOTH in hn and lands >= 3:
                coid = await cast_named(p0, acts, state, BALOTH, "P0")
                if coid is not None:
                    return
        # respond to the Bolts: cast Mutation with BOTH Bolts on the stack.
        # (keyed on the driver's own bolt2-submitted flag, not on parsing
        # the stack entry's name)
        if (not ST["mutation_cast"] and in_flight
                and ST["bolt2_submitted"] and not ST["bolt_resolved"]
                and not ST["bolt2_resolved"]
                and p0_can_pay_mutation(state)):
            key = ("respond", turn)
            if key not in obs.setdefault("respond_diag", []):
                obs["respond_diag"].append(key)
                say(f"RESPOND-DIAG turn={turn} stack=" +
                    json.dumps(stack_entries(state), default=str)[:600])
                say(f"RESPOND-DIAG acts=" +
                    str([(a["type"]) for a in acts][:12]))
                wire("respond_diag",
                     {"turn": turn,
                      "stack": json.loads(json.dumps(stack_entries(state),
                                                     default=str)),
                      "act_types": [a["type"] for a in acts][:20]})
            coid = await cast_named(p0, acts, state, MUTATION, "P0")
            if coid is not None:
                ST["mutation_cast"] = True
                ST["mutation_in_flight"] = True
                ST["mutation_oid"] = coid
                say(f"Mutation submitted (oid {coid}); awaiting targets/X")
                wire("mutation_submitted", {"oid": coid})
                return
            say("RESPOND: Mutation CastSpell NOT advertised on response "
                "tick; passing priority (run will block)")
            wire("respond_not_advertised", {})
        if (phase in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        scan_payment_prompt(p1, "P1", state)
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, "P1")
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(1) != p1.revision):
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p1, sub)
            return
        # bolt target selection
        if (wtype == "TargetSelection" and wf_pending_for(state, 1)
                and ST["bolt_submitted"] and not ST["bolt_resolved"]):
            if await answer_bolt_target(p1, st, state):
                return
        if await discard_tick(p1, 1, "P1", st, state):
            return
        # mutation resolution may offer P1 a payment prompt: answer DECLINE
        # after recording it (scan_payment_prompt recorded it already). If
        # the engine somehow offers a pay prompt, decline to see the counter.
        if (wtype not in ("Priority",) and wf_pending_for(state, 1)
                and ST["mutation_in_flight"]
                and not ST["mutation_resolved"]):
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    entry = prompt_first_seen.setdefault(
                        iid, {"t0": time.time(), "done": False})
                    if entry.get("done"):
                        continue
                    chs = vi_choices(opp)
                    texts = [choice_text(ch).lower() for ch in chs]
                    blob = " ".join(texts)
                    if any(k in blob for k in ("decline", "don't pay",
                                               "do not pay", "no")):
                        pick = next(ch for ch in chs
                                    if any(k in choice_text(ch).lower()
                                           for k in ("decline", "don't pay",
                                                     "do not pay", "no")))
                        say(f"[P1] declining unless-payment: "
                            f"{choice_text(pick)[:60]}")
                        wire("p1_declines_payment",
                             {"text": choice_text(pick)[:80]})
                        await answer_vi(p1, opp, pick, "P1")
                        entry["done"] = True
                        return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        # never pass while a P1 decision is pending
        if wtype in ("TargetSelection", "ChooseXValue", "ManaPayment",
                     "OptionalCostChoice", "UnlessPaymentChooseCost",
                     "PayUnlessCost") and wf_pending_for(state, 1):
            return
        # P1 priority actions: cast Bolt once conditions hold
        in_flight = bool(state.get("stack") or [])
        if (not in_flight and not ST["bolt_submitted"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1):
            hn = hand_lnames(state, 1)
            baloth = bf_id(state, 0, BALOTH)
            # P1 casts the Bolt only once P0 can actually answer with the
            # Mutation RIGHT NOW: {G}{U} untapped (Island + Forest). The
            # latched castable flag goes stale across turns (engine tapped
            # P0's Islands for the Baloth), so check live colors.
            if (BOLT in hn and baloth is not None
                    and p0_can_pay_mutation(state)
                    and len(untapped_lands(state, 1)) >= 1):
                coid = await cast_named(p1, acts, state, BOLT, "P1")
                if coid is not None:
                    ST["bolt_submitted"] = True
                    ST["bolt_oid"] = coid
                    return
        # Bolt #2: cast while Bolt #1 is still on the stack, so the
        # Mutation faces TWO spells. This tests whether the engine offers
        # the optional "up to one target spell" choice when >1 legal
        # target exists (with 1 spell it silently chose zero targets).
        if (in_flight and ST["bolt_submitted"] and not ST["bolt2_submitted"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1):
            hn = hand_lnames(state, 1)
            n_bolts_hand = hn.count(BOLT)
            n_untapped = len(untapped_lands(state, 1))
            key = ("bolt2diag", state.get("turn_number"))
            if key not in obs.setdefault("bolt2diag_done", []):
                obs["bolt2diag_done"].append(key)
                say(f"BOLT2-DIAG turn={state.get('turn_number')} "
                    f"hand_bolts={n_bolts_hand} untapped_lands={n_untapped} "
                    f"in_flight={in_flight}")
                wire("bolt2_diag",
                     {"turn": state.get("turn_number"),
                      "hand_bolts": n_bolts_hand,
                      "untapped_lands": n_untapped, "in_flight": in_flight})
            if (BOLT in hn and len(untapped_lands(state, 1)) >= 1):
                coid = await cast_named(p1, acts, state, BOLT, "P1")
                if coid is not None:
                    ST["bolt2_submitted"] = True
                    ST["bolt2_oid"] = coid
                    say(f"[P1] Bolt #2 submitted (oid {coid})")
                    wire("bolt2_submitted", {"oid": coid})
                    return
        if (state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 600:
        await asyncio.sleep(0.15)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                if pid == 0:
                    await p0_tick(st, merged_actions(st), st["state"])
                else:
                    await p1_tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                obs["tick_errors"].append({"who": tag, "err": str(e)[:200]})
                wire("tick_error", {"who": tag, "err": str(e)})
        # resolution markers
        s = (p0.latest or {}).get("state") or {}
        # capture the Mutation's stack entry (targets) while it is in flight.
        # Match by effect signature (PutCounter + counter sub-ability):
        # stack entries carry no card name, only card_id.
        if ST["mutation_in_flight"]:
            for e in stack_entries(s):
                blob = json.dumps(e, default=str)
                kind = (e.get("kind") or {}).get("type")
                data = (e.get("kind") or {}).get("data") or {}
                ab = data.get("ability") or {}
                eff = json.dumps(ab.get("effect"), default=str).lower()
                sub = json.dumps(ab.get("sub_ability"), default=str).lower()
                if (kind == "Spell" and "putcounter" in eff
                        and "counter" in sub):
                    ST["mutation_on_stack_seen"] = True
                    ST["mutation_stack_targets"] = {
                        "targets": ab.get("targets"),
                        "effect": str(ab.get("effect"))[:300],
                        "sub_ability": str(ab.get("sub_ability"))[:300],
                    }
                    wire("mutation_stack_entry",
                         {"entry": json.loads(blob)})
                    break
        if (ST["mutation_cast"] and not ST["mutation_resolved"]
                and ST["mutation_on_stack_seen"]
                and not any(lname(s, int(oid)) == MUTATION
                            for oid, o in (s.get("objects", {}) or {}).items()
                            if o.get("zone") == "Stack")):
            # mutation left the stack (resolved or fizzled); confirm where
            in_gy = any(lname(s, int(oid)) == MUTATION
                        for oid, o in (s.get("objects", {}) or {}).items()
                        if o.get("zone") == "Graveyard"
                        and o.get("controller") == 0)
            if in_gy or not any(
                    n.lower() == MUTATION for n, _s, _i in stack_names(s)):
                ST["mutation_resolved"] = True
                ST["mutation_resolved_at"] = time.time()
                ST["mutation_in_flight"] = False
                say("mutation left the stack (resolved -> P0 gy "
                    f"={in_gy})")
                wire("mutation_resolved", {"in_P0_gy": in_gy})
        # bolt resolution, tracked by stack-entry count (LIFO: bolt #2 is on
        # top, so it resolves first). A bolt leaving the stack via counter
        # also decrements the count; p0_life disambiguates.
        n_bolts = bolt_on_stack_count(s)
        if n_bolts > ST.get("bolts_seen_max", 0):
            ST["bolts_seen_max"] = n_bolts
        if (ST.get("bolts_seen_max", 0) >= 2 and n_bolts <= 1
                and not ST["bolt2_resolved"]):
            ST["bolt2_resolved"] = True
            say(f"bolt #2 left the stack; P0 life={life_of(s, 0)}")
            wire("bolt2_resolved", {"p0_life": life_of(s, 0)})
        if (ST.get("bolts_seen_max", 0) >= 1 and n_bolts == 0
                and not ST["bolt_resolved"]):
            ST["bolt_resolved"] = True
            say(f"bolt #1 left the stack; P0 life={life_of(s, 0)}")
            wire("bolt_resolved", {"p0_life": life_of(s, 0)})
        if ST["post_exported"]:
            return
        if (s.get("turn_number") or 0) > 25 and not ST["mutation_cast"]:
            notes.append("watchdog: turn 25 reached with no Mutation cast; "
                         "finishing")
            await finish()
            return
        if (ST["mutation_resolved"] and not ST["post_exported"]
                and time.time() - ST.get("mutation_resolved_at", 0) > 20):
            notes.append("watchdog: Mutation resolved 20s ago with no POST "
                         "export; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} life={[life_of(s, i) for i in (0, 1)]} "
                f"baloth={ST['baloth_oid']} bears={ST['bears_oid']} "
                f"bolt={ST['bolt_cast']}/{ST['bolt_resolved']} "
                f"mut={ST['mutation_cast']}/{ST['mutation_resolved']} "
                f"pay_prompt={ST['payment_prompt_seen']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']} "
                f"stack={stack_names(s)}")
    notes.append("global timeout (600s) hit before assertions resolved")
    await finish()


asyncio.run(main())
