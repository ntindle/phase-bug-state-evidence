#!/usr/bin/env python3
"""Issue #7163: Birthing Ritual does not work with more than 1 creature in play.

Re-validation on the pinned release v0.101.0 (build acafe9b, protocol 103).
Prior run: v0.82.0 (build 060b5d2, protocol 70, run 20260914-7163d,
verdict reproduced): the may-sacrifice completed but the put-a-creature-
from-the-7 onto the battlefield step never happened (A6/A7 failed).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json, v0.101.0):
  Birthing Ritual {2}{G}, Enchantment.
  Oracle: "At the beginning of your end step, if you control a creature, look
  at the top seven cards of your library. Then you may sacrifice a creature. If
  you do, you may put a creature card with mana value X or less from among those
  cards onto the battlefield, where X is 1 plus the sacrificed creature's mana
  value. Put the rest on the bottom of your library in a random order."

Reported failure: with >1 creature in play the ability cannot complete its
selection flow - the sacrifice completes but the look-at-top-7 / put-creature-
onto-battlefield step never happens.

Setup:
  P0: 20x Birthing Ritual, 20x Grizzly Bears, 20x Forest.
  P1: 12x Grizzly Bears, 48x Forest (draw-go, zero attackers).
  Both keep 7. Zero-attacker combat scripted on both seats so combat cannot
  mask the trigger.

Trigger leg: at P0's end step with Birthing Ritual + >=2 Bears on the
battlefield, the trigger fires. The driver declines the may-sacrifice until
P0 controls 2+ bears, so the single recorded accept cycle is the
multi-creature case from the report. Expected on a correct engine: sacrifice
selection offers >=2 bears; after the sacrifice the engine offers the
put-a-creature-from-the-7 onto the battlefield pick; answering it puts the
chosen bear on the BF, the rest of the 7 go to the bottom, library shrinks
by exactly 1, stack empties.

Assertions:
  A1_parse           - v0.101.0 AST: Phase/End -> Dig7 prior-look -> optional
                       Sacrifice(1 creature you control) ->
                       EffectOutcome/OptionalEffectPerformed -> Dig(keep1,
                       up_to, Battlefield, Creature mv<=CostPaidObject+1,
                       source prior_look), else Dig(0 keep, rest->Library)
  A2_setup_ok        - Birthing Ritual on P0 BF, >=2 Bears on P0 BF at the End
                       step trigger (pre.json exported with trigger on stack
                       or at the may-sacrifice decision point)
  A3_trigger_fired   - ritual trigger fires at P0's End step
  A4_sacrifice       - may-sacrifice prompt offered; the selection offers BOTH
                       bears (>=2 candidates, the reported multi-creature
                       case); accept answered
  A5_sacrifice_effect- chosen bear reaches the Graveyard (sacrifice completes;
                       X = 1 + sacrificed bear mv 2 = 3)
  A6_bf_pick         - after the sacrifice, the engine offers the
                       put-a-creature-from-the-7 onto the battlefield choice
                       (REPORTED BUG: this step never happens)
  A7_cleanup         - if the pick is offered+answered: the chosen looked-at
                       bear is on the Battlefield, the sacrificed bear is in
                       the Graveyard, library shrank by exactly 1 (1 of 7 to
                       BF, 6 to the bottom), stack empty

Driver conventions (protocol 103, from scenario_650_01010.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
    (driver/client.py handles this).
  - MulliganDecision answered as-is, gated on pending[] mulligan_count
    Declare entries ({"choice":{"type":"Keep"}}).
  - DiscardToHandSize answered through viewer_interaction (the legacy
    SelectCards path is silently ignored on protocol 103).
  - OptionalEffectChoice answered from exactChoices decideOptionalEffect
    role/value surfaces with {"type":"choose","data":{"choiceId":...}}.
  - Candidate picks answered from the advertised viewer_interaction
    opportunity per its response schema (exactChoices -> choose/choiceId;
    schema spec type -> sequence/select with choiceIds); candidates are
    classified by their referenced object's zone, never by list position.
  - PassPriority: legacy action first, then the viewer_interaction
    passPriority action-code fallback.
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    reading fresh state and evaluating the trigger window (leg-engagement
    race fix). Export-only checkpoints fall through to the priority pass in
    the same tick - never `return` after an export while holding priority.
  - Revision-gated main loop with a 5s re-tick safety net for a client
    holding priority with no revision change.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261004-7163b"
ISSUE = 7163
SERVER_RUN_DIR = "runs/20261004-7163"  # backfill-owned server for this run
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.101.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned v0.101.0 server on 127.0.0.1:9374 "
                      "(started fresh by this run on runs/20261004-7163; "
                      "raw ServerHello verified 0.101.0/acafe9b/103)"),
    "source": ("2026-10-04: latest stable release v0.101.0 (published "
               "2026-10-03) == pinned release dir; ServerHello "
               "0.101.0/acafe9b/protocol 103 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))

RITUAL = "birthing ritual"
BEARS = "grizzly bears"
FOREST = "forest"

P0_DECK = deck((RITUAL, 20), (BEARS, 20), (FOREST, 20))
P1_DECK = deck((BEARS, 12), (FOREST, 48))

DEADLINE_S = 1500
ASS_KEYS = ("A1_parse", "A2_setup_ok", "A3_trigger_fired", "A4_sacrifice",
            "A5_sacrifice_effect", "A6_bf_pick", "A7_cleanup")

# waiting_for types that are never candidate-bearing decisions for this driver
NON_DECISION_WF = {"Priority", "MulliganDecision", "DiscardToHandSize",
                   "DeclareAttackers", "DeclareBlockers", "ChooseLegend",
                   "OptionalEffectChoice"}

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")


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


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def wf_of(state):
    return state.get("waiting_for") or {}


def pending_for(state, pid):
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def can_pay(state, pid, colors=(), generic=0):
    color_of = {FOREST: "G"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def ref_key(ref):
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


def cand_name(state, ch):
    ref = ref_key(cand_ref(ch))
    if ref is not None:
        return obj_lname(state, ref)
    return choice_text(ch).lower()


def cand_zone(state, ch):
    ref = ref_key(cand_ref(ch))
    if ref is None:
        return None, None
    o = get_obj(state, ref)
    return ref, (o.get("zone") if o else None)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={str(cid)[:12]} "
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.101.0"), f"unexpected version {ver}"
    assert int(proto) == 103, f"unexpected protocol {proto}"
    assert str(build) == "acafe9b", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    """A1: verify the pinned v0.101.0 card-data parse of Birthing Ritual."""
    ok, detail = True, []
    try:
        e = CARD_DATA[RITUAL]
        with open(f"{EVDIR}/parse_birthing_ritual.json", "w") as f:
            json.dump(e.get("triggers"), f, indent=1)
        trig = (e.get("triggers") or [])[0]
        mode_ok = trig.get("mode") == "Phase" and trig.get("phase") == "End"
        ex = trig.get("execute") or {}
        dig1 = ex.get("effect") or {}
        dig1_ok = (dig1.get("type") == "Dig"
                   and (dig1.get("count") or {}).get("type") == "Fixed"
                   and (dig1.get("count") or {}).get("value") == 7
                   and dig1.get("rest_destination") == "Library")
        sac = ex.get("sub_ability") or {}
        seff = sac.get("effect") or {}
        sac_ok = (seff.get("type") == "Sacrifice"
                  and (seff.get("target") or {}).get("type") == "Typed"
                  and "Creature" in ((seff.get("target") or {})
                                     .get("type_filters") or [])
                  and sac.get("optional") is True)
        sub2 = sac.get("sub_ability") or {}
        cond = sub2.get("condition") or {}
        cond_ok = (cond.get("type") == "EffectOutcome"
                   and cond.get("signal") == "OptionalEffectPerformed")
        dig2 = sub2.get("effect") or {}
        filt = dig2.get("filter") or {}
        props = filt.get("properties") or []
        cmc = next((p for p in props if p.get("type") == "Cmc"), {})
        cmcval = cmc.get("value") or {}
        inner = cmcval.get("inner") or {}
        dig2_ok = (dig2.get("type") == "Dig"
                   and dig2.get("destination") == "Battlefield"
                   and dig2.get("keep_count") == 1
                   and dig2.get("up_to") is True
                   and dig2.get("source") == "prior_look"
                   and "Creature" in (filt.get("type_filters") or [])
                   and cmc.get("comparator") == "LE"
                   and cmcval.get("type") == "Offset"
                   and cmcval.get("offset") == 1
                   and (inner.get("qty") or {}).get("type") == "ObjectManaValue"
                   and ((inner.get("qty") or {}).get("scope") or {})
                   .get("type") == "CostPaidObject")
        els = sub2.get("else_ability") or {}
        edig = els.get("effect") or {}
        else_ok = (edig.get("type") == "Dig"
                   and edig.get("rest_destination") == "Library"
                   and edig.get("keep_count") == 0)
        ok = mode_ok and dig1_ok and sac_ok and cond_ok and dig2_ok and else_ok
        detail.append(f"mode={mode_ok} dig1={dig1_ok} sac={sac_ok} "
                      f"cond={cond_ok} dig2={dig2_ok} else={else_ok}")
    except Exception as ex_:
        ok = False
        detail.append(f"parse check raised: {ex_}")
    ST["ass"]["A1_parse"] = "passed" if ok else "failed"
    ST["notes"].append("A1_parse: " + ("passed" if ok else "FAILED")
                       + " (" + "; ".join(detail) + ")")
    say(ST["notes"][-1])
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "detail": detail,
                   "oracle": str(CARD_DATA.get(RITUAL, {})
                                 .get("oracle_text"))[:400]}, f, indent=1)


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    key = (tag, "mull", str(pend.get("mulligan_count", "?")))
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = hand_lnames(state, pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard_to_handsize(c, pid, tag):
    """Protocol-103 rule: DiscardToHandSize goes through viewer_interaction;
    the legacy SelectCards path is silently ignored by the engine."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    wfd = wf_of(state).get("data") or {}
    if str(wfd.get("player", pid)) != str(pid):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"

        def rank(ch):
            nm = cand_name(state, ch)
            if nm == FOREST:
                return (0, nm)
            if nm in (RITUAL, BEARS):
                return (9, nm)
            return (5, nm)

        picks = [ch["id"] for ch in sorted(cands, key=rank)[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    say(f"[{tag}] WARNING: DiscardToHandSize without viewer_interaction; not answering")
    return False


async def pay_tick(c, acts):
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
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
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


async def try_cast(c, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if a:
        ST["mana_needs"][tag] = dict(needs)
        say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
        wire("cast", {"who": tag, "card": name, "oid": oid})
        await submit_as_is(c, a)
        return True
    # fallback: viewer_interaction exactChoices castSpell menu
    for opp in vi_ops(c.latest):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "castSpell" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and "reference" in s.get("data", {})]
            for r in refs:
                if obj_lname(state, r) == name:
                    ST["mana_needs"][tag] = dict(needs)
                    say(f"[{tag}] casting {name} via vi castSpell (ref {r})")
                    wire("cast", {"who": tag, "card": name, "ref": r,
                                  "via": "vi"})
                    await answer_vi(c, opp, ch, tag)
                    return True
    return False


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
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
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if sum(needs.values()) <= 0:
            continue
        pick, used = None, None
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def answer_optional(c, opp, accept, tag, why):
    """Answer an OptionalEffectChoice-style opportunity from its
    decideOptionalEffect role/value surfaces (protocol 103: choice text is
    empty, the role/value surfaces carry accept=true|false)."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    choices = data.get("choices") or data.get("candidates") or []
    wire("optional_choices",
         {"tag": tag, "why": why, "iid": str(iid)[:16],
          "n_choices": len(choices),
          "choices": [{"id": ch.get("id"),
                       "text": str(ch.get("text"))[:60],
                       "surfaces": ch.get("surfaces")} for ch in choices]})
    pick = None
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            role = str(d.get("role", "")).lower()
            val = str(d.get("value", "")).lower()
            if role == "accept" and val == ("true" if accept else "false"):
                pick = ch
                break
        if pick:
            break
    if pick is None:
        wire("optional_no_accept_surface", {"tag": tag, "why": why})
        say(f"[{tag}] optional '{why}': no accept={accept} surface found")
        return False
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": pick["id"]}}}, tag)
    say(f"[{tag}] optional '{why}' answered accept={accept}")
    wire("optional_answer", {"tag": tag, "why": why, "accept": accept,
                             "choice_id": pick.get("id")})
    return True


async def submit_candidate_choice(c, opp, choice_ids, tag, why):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    rtype = resp.get("type")
    if rtype == "exactChoices":
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": choice_ids[0]}}}
    else:
        stype = (data.get("spec", {}) or {}).get("type") or "sequence"
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": stype,
                            "data": {"choiceIds": list(choice_ids)}}}
    wire("candidate_answer", {"tag": tag, "why": why,
                              "choice_ids": [str(x)[:12] for x in choice_ids]})
    say(f"[{tag}] candidate answer '{why}': {len(choice_ids)} choice(s)")
    await interact_as(c, sub, tag)

async def export_named(c, tag):
    try:
        raw = await c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(raw)
        say(f"exported {tag.upper()}")
        wire(f"export_{tag}", {"ok": True})
        return True
    except Exception as ex2_:
        ST["notes"].append(f"{tag} export failed: {ex2_}")
        return False


async def export_pre(c, label, force=False):
    # pre = state at the start of the trigger's resolution window: either
    # the trigger on the stack (ideal) or the may-sacrifice decision point
    # (inferred path). Either way it precedes the sacrifice, which is the
    # operation under investigation. force=True re-exports so pre always
    # describes the recorded accept cycle, not an earlier declined one.
    if ST["pre_exported"] and not force:
        return True
    if await export_named(c, "pre"):
        ST["pre_exported"] = True
        pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
        ST["pre_bears_bf"] = len(bf_by_name(pre_st, 0, BEARS))
        ST["pre_lib_count"] = len([o for o in (pre_st.get("objects") or {}).values()
                                   if o.get("zone") == "Library"
                                   and str(o.get("owner", -1)) == "0"])
        say(f"[P0] pre ({label}): bears_bf={ST['pre_bears_bf']} "
            f"lib={ST['pre_lib_count']}")
        return True
    return False


def ritual_trigger_on_stack(state):
    for se in stack_entries(state):
        blob = json.dumps(se).lower()
        if "birthing ritual" in blob and "trigger" in blob:
            return True
    return False


def lib_count(state, pid):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Library"
               and str(o.get("owner", -1)) == str(pid))


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    if await do_mulligan(c, pid, tag):
        return
    if await do_discard_to_handsize(c, pid, tag):
        return
    acts = merged_actions(st)
    wtype = wf_of(state).get("type")
    wf = wf_of(state)
    lw = ST.setdefault("last_wf", {})
    if lw.get(pid) != (wtype, str((wf.get("data") or {}).get("player"))):
        lw[pid] = (wtype, str((wf.get("data") or {}).get("player")))
        wire("wf_transition", {"tag": tag, "type": wtype,
                               "player": str((wf.get("data") or {}).get("player")),
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase"),
                               "n_opps": len(vi_ops(st))})
        if wtype not in ("Priority", "DeclareAttackers",
                         "DeclareBlockers", "MulliganDecision",
                         "DiscardToHandSize"):
            for opp in vi_ops(st):
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                chs = data.get("choices") or data.get("candidates") or []
                wire("wf_opp_detail",
                     {"tag": tag, "iid": str(opp.get("interactionId"))[:16],
                      "rtype": resp.get("type"),
                      "spec": (data.get("spec", {}) or {}).get("type"),
                      "n_choices": len(chs),
                      "sample": [{"name": cand_name(state, ch),
                                  "zone": cand_zone(state, ch)[1]}
                                 for ch in chs[:10]]})
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return
    # combat: never attack, never block
    if wtype in ("DeclareAttackers", "DeclareBlockers") \
            and str((wf.get("data") or {}).get("player")) == str(pid):
        da = next((a for a in acts if a["type"] == wtype), None)
        if da:
            sub = copy.deepcopy(da)
            sub.setdefault("data", {})
            if wtype == "DeclareAttackers":
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
            else:
                sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say(f"[{tag}] {wtype}: empty")
        return
    if await pay_mana_vi(c, st, tag):
        return

    # ---- priority gate, then yield before the trigger-window evaluation
    await asyncio.sleep(0)  # yield to the pump before reading fresh state
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    wtype = wf_of(state).get("type")
    wf = wf_of(state)
    wf_player = str((wf.get("data") or {}).get("player"))

    # ritual trigger tracking (P0's view)
    if ritual_trigger_on_stack(state) and not ST["trigger_fired"]:
        ST["trigger_fired"] = True
        ST["ritual_turn"] = state.get("turn_number")
        wire("trigger_fired", {"turn": ST["ritual_turn"],
                               "phase": state.get("phase")})
        say(f"[P0] Birthing Ritual trigger on stack (turn {ST['ritual_turn']})")
        await export_pre(c, "trigger on stack")
    if ST["trigger_fired"] and not ST["trigger_resolved"] \
            and not ritual_trigger_on_stack(state):
        ST["trigger_resolved"] = True
        wire("trigger_resolved", {"turn": state.get("turn_number"),
                                  "sac_done": ST["sac_done"],
                                  "bf_prompt_seen": ST["bf_prompt_seen"]})
        say(f"[P0] ritual trigger left the stack "
            f"(sac_done={ST['sac_done']} bf_prompt_seen={ST['bf_prompt_seen']})")

    # ---- optional prompts ----
    # Cycle-aware: the recorded accept cycle is the turn-10 (first accepted)
    # sacrifice. A may-sacrifice arriving on a LATER turn is a new trigger -
    # decline it so the game keeps moving and the recorded cycle stays
    # isolated. The may-put-onto-battlefield of the recorded cycle can only
    # arrive in the SAME end step as its sacrifice.
    if wtype == "OptionalEffectChoice" and wf_player == str(pid):
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS:
                continue
            turn = state.get("turn_number")
            phase = state.get("phase")
            if not ST["sac_accepted"] and (ST["trigger_fired"] or ST["ritual_on_bf"]):
                # may-sacrifice. The trigger window is fast; a
                # may-sacrifice OptionalEffectChoice for P0 with the Ritual
                # on the battlefield can only be this trigger resolving (no
                # other optional effects exist in the fixture), so answer it
                # and record the inference if the stack sample missed it.
                ST["sac_prompt_seen"] = True
                if not ST["trigger_fired"]:
                    ST["trigger_fired"] = True
                    ST["trigger_inferred"] = True
                    ST["ritual_turn"] = state.get("turn_number")
                    wire("trigger_fired_inferred",
                         {"turn": ST["ritual_turn"],
                          "phase": state.get("phase")})
                    say(f"[P0] Birthing Ritual trigger inferred from "
                        f"may-sacrifice prompt (turn {ST['ritual_turn']})")
                # The reported bug needs >1 creature. Decline the sacrifice
                # until P0 controls 2+ bears, so the single recorded accept
                # cycle is the multi-creature case.
                n_bears = len(bf_by_name(state, 0, BEARS))
                accept = n_bears >= 2
                if accept:
                    await export_pre(c, "may-sacrifice decision point",
                                     force=True)
                if await answer_optional(
                        c, opp, accept, tag,
                        f"ritual may-sacrifice (bears_bf={n_bears})"):
                    SUBMITTED_OPPS.add(iid)
                    if accept:
                        ST["sac_accepted"] = True
                    else:
                        ST["sac_declines"] += 1
                        say(f"[P0] declined may-sacrifice with "
                            f"{n_bears} bear(s) on BF; waiting for 2+")
                return
            if ST["sac_done"] and not ST["bf_answered"] \
                    and turn == ST["sac_done_turn"] and phase == "End":
                # may-put-onto-battlefield of the RECORDED cycle: it can only
                # arrive in the same end step as its sacrifice.
                ST["bf_prompt_seen"] = True
                if await answer_optional(c, opp, True, tag,
                                         "ritual may-put-onto-bf"):
                    SUBMITTED_OPPS.add(iid)
                    ST["bf_accepted"] = True
                return
            # Otherwise: a subsequent trigger's may-sacrifice (the recorded
            # cycle is already accepted) or an unexpected optional - decline
            # so the game keeps moving; never leave it unanswered.
            say(f"[{tag}] declining subsequent/unexpected OptionalEffectChoice "
                f"(turn {turn} phase {phase})")
            if await answer_optional(c, opp, False, tag,
                                     "decline subsequent optional"):
                SUBMITTED_OPPS.add(iid)
                ST["later_declines"] += 1
            return
        return

    # ---- candidate-bearing opportunities (sacrifice pick / bf pick) ----
    # Answer only live decisions for P0; viewer_interaction can retain a
    # previous decision's opportunities into the following Priority window,
    # and answering those would be acting on a stale prompt.
    if wf_player == str(pid) and wtype not in NON_DECISION_WF:
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            choices = data.get("choices") or data.get("candidates") or []
            if not choices:
                continue
            zones = {}
            for ch in choices:
                _k, z = cand_zone(state, ch)
                zones[z] = zones.get(z, 0) + 1
            # sacrifice selection: candidates are P0's BF bears. Only for the
            # recorded cycle - once its sacrifice is done, a BF-bear
            # selection must belong to a declined subsequent trigger.
            if ST["sac_accepted"] and not ST["sac_answered"] \
                    and not ST["sac_done"] \
                    and zones.get("Battlefield", 0) >= 1:
                bear_ids = []
                for ch in choices:
                    k, z = cand_zone(state, ch)
                    if z != "Battlefield":
                        continue
                    o = get_obj(state, k)
                    if o and obj_lname(state, k) == BEARS \
                            and str(o.get("controller", -1)) == "0":
                        bear_ids.append((ch["id"], k))
                ST["sac_candidates_n"] = len(bear_ids)
                wire("sac_candidates",
                     {"n_total": len(choices), "n_bears": len(bear_ids),
                      "zones": zones, "wtype": wtype})
                say(f"[P0] sacrifice selection: {len(bear_ids)} bear "
                    f"candidates of {len(choices)} choices (wtype={wtype})")
                if bear_ids:
                    cid, k = bear_ids[0]
                    await submit_candidate_choice(c, opp, [cid], tag,
                                                  "ritual sacrifice")
                    SUBMITTED_OPPS.add(iid)
                    ST["sac_answered"] = True
                    ST["sac_chosen_oid"] = int(k)
                    ST["sac_chosen_name"] = BEARS
                    say(f"[P0] sacrificed bear oid={k}")
                return
            # battlefield pick: candidates are the looked-at 7 (not on the
            # battlefield). Gate on sac_done so the auto-sacrifice path,
            # where no selection prompt was ever answered, is still handled.
            if ST["sac_done"] and not ST["bf_answered"] \
                    and zones.get("Battlefield", 0) == 0 \
                    and len(choices) >= 1:
                lim_ids = []
                for ch in choices:
                    k, z = cand_zone(state, ch)
                    if z == "Battlefield":
                        continue
                    if cand_name(state, ch) == BEARS:
                        lim_ids.append((ch["id"], k, z))
                ST["bf_prompt_seen"] = True
                ST["bf_candidates_n"] = len(lim_ids)
                wire("bf_candidates",
                     {"n_total": len(choices), "n_bears": len(lim_ids),
                      "zones": zones, "wtype": wtype,
                      "sample_zones": [z for _, _, z in lim_ids[:7]]})
                say(f"[P0] BF-pick selection: {len(lim_ids)} bear "
                    f"candidates of {len(choices)} choices, zones={zones} "
                    f"(wtype={wtype})")
                # mid checkpoint, then fall through to the priority pass
                # below in the same tick - never return after a bare export.
                if not ST["mid_exported"]:
                    await export_named(c, "mid")
                    ST["mid_exported"] = True
                if lim_ids:
                    cid, k, z = lim_ids[0]
                    await submit_candidate_choice(c, opp, [cid], tag,
                                                  "ritual bf-pick")
                    SUBMITTED_OPPS.add(iid)
                    ST["bf_answered"] = True
                    ST["bf_chosen_oid"] = int(k)
                    say(f"[P0] put bear oid={k} (zone {z}) onto BF")
                return
        # a candidate prompt we did not classify: hold, log only
        wire("unclassified_decision", {"tag": tag, "wtype": wtype})

    # sacrifice-done detection: a P0-controlled bear leaving the Battlefield
    # for the Graveyard during the resolution window. Handles both the
    # answered-selection path and the engine's single-candidate
    # auto-sacrifice, which presents no selection prompt at all.
    cur_bf = set(bf_by_name(state, 0, BEARS))
    prev_bf = ST.get("prev_bf_bears")
    if prev_bf is not None and ST["sac_accepted"] and not ST["sac_done"]:
        for g in sorted(prev_bf - cur_bf):
            o = get_obj(state, g)
            if o and o.get("zone") == "Graveyard":
                ST["sac_done"] = True
                ST["sac_done_turn"] = state.get("turn_number")
                ST["sac_done_at"] = time.time()
                if ST["sac_chosen_oid"] is None:
                    ST["sac_chosen_oid"] = g
                    ST["sac_chosen_name"] = BEARS
                    ST["sac_auto"] = True
                wire("sac_done", {"oid": g, "turn": ST["sac_done_turn"],
                                  "auto": ST["sac_auto"]})
                say(f"[P0] sacrificed bear oid={g} reached Graveyard"
                    + (" (auto-sacrifice: no selection prompt was offered)"
                       if ST["sac_auto"] else ""))
                break
    ST["prev_bf_bears"] = cur_bf
    # bf-pick done detection
    if ST["bf_answered"] and not ST["bf_done"]:
        co = ST["bf_chosen_oid"]
        o = get_obj(state, co)
        if o and o.get("zone") == "Battlefield":
            ST["bf_done"] = True
            wire("bf_done", {"oid": co, "turn": state.get("turn_number")})
            say(f"[P0] chosen bear oid={co} is on the Battlefield")

    if ST["finished"]:
        return

    # main-phase economy: land first, then cast (ritual once, bears freely)
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        hn = hand_lnames(state, 0)
        rituals_bf = bf_by_name(state, 0, RITUAL)
        if rituals_bf:
            ST["ritual_on_bf"] = True
        if not rituals_bf and RITUAL in hn and can_pay(state, 0, ("G",), 2):
            if await try_cast(c, state, acts, tag, RITUAL,
                              {"G": 1, "generic": 2}):
                return
        if BEARS in hn and can_pay(state, 0, ("G",), 1):
            if await try_cast(c, state, acts, tag, BEARS,
                              {"G": 1, "generic": 1}):
                return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    if await do_mulligan(c, pid, tag):
        return
    if await do_discard_to_handsize(c, pid, tag):
        return
    acts = merged_actions(st)
    wtype = wf_of(state).get("type")
    wf = wf_of(state)
    if wtype == "DeclareAttackers" \
            and str((wf.get("data") or {}).get("player")) == str(pid):
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers" \
            and str((wf.get("data") or {}).get("player")) == str(pid):
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        if BEARS in hand_lnames(state, 1) and can_pay(state, 1, ("G",), 1):
            if await try_cast(c, state, acts, tag, BEARS,
                              {"G": 1, "generic": 1}):
                return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)

async def finalize(c):
    ass = ST["ass"]
    notes = ST["notes"]
    states = {}
    for fn in ("pre", "mid", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as ex3:
            notes.append(f"state reload failed for {fn}.json: {ex3}")
    pre, mid, post = states.get("pre"), states.get("mid"), states.get("post")
    ref = post or mid or pre

    def objects(s):
        return (s or {}).get("objects", {}) or {}

    def lname_of(o):
        return str(o.get("base_name") or o.get("name") or "").lower()

    def bf_bears(s):
        return [int(oid) for oid, o in objects(s).items()
                if o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "0"
                and lname_of(o) == BEARS]

    # ---- A2: setup ----
    if pre is not None:
        ok = (any(o.get("zone") == "Battlefield"
                  and str(o.get("controller", -1)) == "0"
                  and lname_of(o) == RITUAL
                  for o in objects(pre).values())
              and len(bf_bears(pre)) >= 2)
        ass["A2_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A2: ritual_bf={ST['ritual_on_bf']} "
                     f"pre_bears_bf={ST['pre_bears_bf']} (>=2 needed) "
                     f"pre_lib={ST['pre_lib_count']}")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 failed: pre.json missing")

    # ---- A3: trigger fired ----
    if ST["trigger_fired"]:
        ass["A3_trigger_fired"] = "passed"
        notes.append(f"A3: trigger_fired at turn {ST['ritual_turn']}, "
                     f"resolved={ST['trigger_resolved']} "
                     f"inferred_from_prompt={ST['trigger_inferred']}")
    else:
        ass["A3_trigger_fired"] = "failed"
        notes.append("A3 failed: ritual trigger never seen on stack")

    # ---- A4: sacrifice prompt + multi-candidate selection ----
    if ST["sac_prompt_seen"]:
        ok = ST["sac_accepted"] and ST["sac_candidates_n"] >= 2
        ass["A4_sacrifice"] = "passed" if ok else "failed"
        notes.append(f"A4: sac_prompt_seen={ST['sac_prompt_seen']} "
                     f"sac_accepted={ST['sac_accepted']} "
                     f"sac_declines={ST['sac_declines']} "
                     f"sac_candidates_n={ST['sac_candidates_n']} "
                     f"(>=2 required for the reported multi-creature case)")
    else:
        ass["A4_sacrifice"] = "failed"
        notes.append("A4 failed: may-sacrifice prompt never offered")

    # ---- A5: sacrifice completes ----
    if ST["sac_done"] and ref is not None:
        co = ST["sac_chosen_oid"]
        o = get_obj(ref, co) if co is not None else {}
        ok = bool(o) and o.get("zone") == "Graveyard"
        ass["A5_sacrifice_effect"] = "passed" if ok else "failed"
        notes.append(f"A5: sac_chosen_oid={co} zone={o.get('zone') if o else None} "
                     f"auto={ST['sac_auto']} (X = 1 + sacrificed bear mv 2 = 3)")
    elif not ST["sac_done"]:
        ass["A5_sacrifice_effect"] = "not-run"
        notes.append("A5 not-run: sacrifice never completed")
    else:
        ass["A5_sacrifice_effect"] = "failed"
        notes.append("A5 failed: no state to check")

    # ---- A6: bf-pick prompt ----
    # Zero observations must never read as "failed": not-run when the
    # sacrifice never completed; failed only when the sacrifice completed
    # and the pick prompt observably never appeared.
    if ST["sac_done"]:
        ok = ST["bf_prompt_seen"]
        ass["A6_bf_pick"] = "passed" if ok else "failed"
        notes.append(f"A6: bf_prompt_seen={ST['bf_prompt_seen']} "
                     f"bf_candidates_n={ST['bf_candidates_n']} "
                     f"(REPORTED BUG: put-a-creature-onto-BF step never "
                     f"happens after the sacrifice)")
    else:
        ass["A6_bf_pick"] = "not-run"
        notes.append("A6 not-run: sacrifice never completed")

    # ---- A7: cleanup / full resolution ----
    if ST["bf_answered"] and post is not None:
        bo = ST["bf_chosen_oid"]
        boo = get_obj(post, bo) if bo is not None else {}
        co = ST["sac_chosen_oid"]
        coo = get_obj(post, co) if co is not None else {}
        lib_post = lib_count(post, 0)
        lib_delta = (ST["pre_lib_count"] or 0) - lib_post
        bf_ok = bool(boo) and boo.get("zone") == "Battlefield"
        sac_ok = bool(coo) and coo.get("zone") == "Graveyard"
        lib_ok = lib_delta == 1
        stack_ok = len(stack_entries(post)) == 0
        ok = bf_ok and sac_ok and lib_ok and stack_ok
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A7: bf_chosen_on_bf={bf_ok} sac_in_gy={sac_ok} "
                     f"lib_delta={lib_delta} (expect 1: 7 looked at, 1 to "
                     f"BF, 6 to bottom) stack_empty={stack_ok}")
    elif ST["stuck"]:
        ass["A7_cleanup"] = "failed"
        notes.append("A7 failed: ability stalled after the sacrifice - "
                     "no BF-pick prompt, game moved on with the 7 cards "
                     "unresolved")
    else:
        ass["A7_cleanup"] = "not-run"
        notes.append("A7 not-run: bf pick never answered")

    for k, v in ass.items():
        say(f"{k}: {v}")

    # verdict: the reported bug is the ability failing to complete with
    # multiple creatures (sacrifice completes, BF-pick step never happens)
    if (ass["A3_trigger_fired"] == "passed"
            and ass["A4_sacrifice"] == "passed"
            and ass["A5_sacrifice_effect"] == "passed"
            and ass["A6_bf_pick"] == "failed"):
        verdict = "reproduced"
    elif (ass["A6_bf_pick"] == "passed"
          and ass["A7_cleanup"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say("VERDICT: " + verdict)

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/7163",
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": SERVER_RUN_DIR,
        "driver": {"protocol_advertised": 103, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": "20x Birthing Ritual + 20x Grizzly Bears + 20x Forest",
                  "P1": "12x Grizzly Bears + 48x Forest"},
        "setup_line": ("P0: 20x Birthing Ritual + 20x Grizzly Bears + 20x Forest | "
                       "P1: 12x Grizzly Bears + 48x Forest (draw-go, zero attackers)"),
        "contract_line": ("End step with Ritual + 2+ Bears: may-sacrifice must offer "
                          ">=2 bears; after the sacrifice the engine must offer the "
                          "put-a-creature-from-the-7 (mv<=X) onto the battlefield pick."),
        "stats": {"max_turn": ST.get("max_turn"), "ritual_turn": ST.get("ritual_turn"),
                  "sac_done": ST.get("sac_done"), "bf_done": ST.get("bf_done"),
                  "sac_declines": ST.get("sac_declines"),
                  "later_declines": ST.get("later_declines")},
        "assertions": ass,
        "notes": notes,
        "driver_state": {k: ST.get(k) for k in
                         ("trigger_fired", "trigger_inferred", "trigger_resolved",
                          "pre_exported", "mid_exported", "post_exported",
                          "sac_prompt_seen", "sac_accepted", "sac_candidates_n",
                          "sac_answered", "sac_chosen_oid", "sac_done",
                          "sac_auto", "sac_declines",
                          "bf_prompt_seen", "bf_candidates_n", "bf_answered",
                          "bf_chosen_oid", "bf_done", "stuck", "later_declines",
                          "ritual_turn", "pre_bears_bf", "pre_lib_count")},
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "Zero-attacker combat was scripted on both seats so combat could not mask the trigger.",
            "Grizzly Bears (mv 2) used as both the sacrifice and the put-onto-BF candidate; X = 3.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7163_01010.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict},
                  f, indent=1)
    try:
        import shutil
        shutil.copy(f"{BACKFILL}/{SERVER_RUN_DIR}/server.log",
                    f"{EVDIR}/server.log")
    except Exception as ex4:
        notes.append(f"server.log copy failed: {ex4}")
    render_png(run)
    # Close the logs BEFORE hashing: nothing may be written to
    # scenario_run.log / wire_log.jsonl after the manifest is computed.
    say("finalize: closing logs, computing manifest")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote manifest.sha256", flush=True)
    for fn in ("pre.json", "mid.json", "post.json",
               "parse_birthing_ritual.json", "run.json", "assertions.json",
               "data_evidence.json"):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            json.load(open(p))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
    for line in man:
        h, fn = line.split("  ")
        assert hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
    print("validation: all JSON parse, PNG readable, hashes match", flush=True)
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1),
          flush=True)


def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7163 - Birthing Ritual: fails with "
           ">1 creature in play", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.101.0 (acafe9b) protocol 103 - 2026-10-04",
           fill=(140, 160, 180))
    y += 28
    col = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: end step -> look at top 7 -> may sacrifice a",
           fill=(200, 210, 225))
    y += 24
    d.text((36, y), "creature -> may put a creature (mv <= X=sac mv+1) "
           "from the 7 onto the BF,", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "rest on the bottom in random order.",
           fill=(200, 210, 225))
    y += 34
    labels = {
        "A1_parse": "PARSE: Phase/End -> Dig7 -> optional Sacrifice -> "
                    "cond Dig(BF, mv<=CostPaidObject+1)",
        "A2_setup_ok": "GAME: ritual on P0 BF, >=2 Bears on P0 BF "
                       "at End-step trigger (pre.json)",
        "A3_trigger_fired": "GAME: ritual trigger fires at P0 End step",
        "A4_sacrifice": "GAME: may-sacrifice offered; BOTH bears "
                        "selectable (>=2 candidates)",
        "A5_sacrifice_effect": "GAME: chosen bear reaches the Graveyard "
                              "(X=3)",
        "A6_bf_pick": "GAME: put-a-creature-from-the-7-onto-BF offered "
                      "(REPORTED BUG: never happens)",
        "A7_cleanup": "GAME: new bear on BF, sac bear in GY, library -1, "
                     "stack empty",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v}", fill=c)
        y += 22
        d.text((52, y), lab[:104], fill=(150, 160, 175))
        y += 26
    y += 8
    ds = run.get("driver_state") or {}
    d.text((24, y), f"trigger_fired={ds.get('trigger_fired')} "
           f"sac_candidates={ds.get('sac_candidates_n')} "
           f"sac_done={ds.get('sac_done')} "
           f"bf_prompt_seen={ds.get('bf_prompt_seen')} "
           f"bf_done={ds.get('bf_done')}", fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


async def main():
    pidfile = "/tmp/scenario_7163_01010.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7163_01010 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


async def _main():
    ST.update({
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "t0": time.time(),
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "trigger_fired": False, "trigger_inferred": False,
        "trigger_resolved": False, "ritual_turn": 0,
        "ritual_on_bf": False, "pre_bears_bf": 0, "pre_lib_count": 0,
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "sac_prompt_seen": False, "sac_accepted": False,
        "sac_candidates_n": 0, "sac_answered": False,
        "sac_chosen_oid": None, "sac_chosen_name": None,
        "sac_declines": 0, "sac_done": False, "sac_done_turn": 0,
        "sac_done_at": 0, "sac_auto": False, "prev_bf_bears": None,
        "bf_prompt_seen": False, "bf_candidates_n": 0,
        "bf_answered": False, "bf_accepted": False,
        "bf_chosen_oid": None, "bf_done": False,
        "stuck": False, "stuck_exported": False,
        "later_declines": 0,
        "cleanup_turns": 0, "done": False, "finished": False,
        "max_turn": 0, "last_prog_key": None, "last_prog_t": time.time(),
        "game_code": None,
    })
    await verify_server_hello()
    check_data_level()

    t0 = ST["t0"]
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    i = 0
    deadline = t0 + DEADLINE_S
    while i < 6000 and time.time() < deadline and not ST["done"]:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                        f"wf={(s0.get('waiting_for') or {}).get('type')}")
                    last_change[c.player_id] = time.time()
                # Safety net: if this client holds priority but produced no
                # revision for a while, re-tick anyway - a tick that
                # returned without submitting must not stall the game.
                wf = st["state"].get("waiting_for") or {}
                holds_prio = (wf.get("type") == "Priority" and str(
                    (wf.get("data") or {}).get("player")) == str(c.player_id))
                if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st_now = p0.latest or {}
        state_now = st_now.get("state", st_now)
        turn_now = state_now.get("turn_number") or 0
        ST["max_turn"] = max(ST["max_turn"], turn_now)
        # general stall guard: a frozen decision (same waiting_for and
        # revision) for >90s means nobody can move the game forward
        prog_key = ((state_now.get("waiting_for") or {}).get("type"),
                    str((state_now.get("waiting_for") or {}).get("data", {})
                        .get("player")), p0.revision)
        if prog_key == ST.get("last_prog_key"):
            if time.time() - ST.get("last_prog_t", t0) > 90:
                ST["notes"].append(f"stalled: waiting_for={prog_key[0]} "
                                   f"player={prog_key[1]} rev={prog_key[2]} "
                                   "frozen >90s; exporting stuck state")
                say(f"STALLED on {prog_key[0]}, exporting stuck state")
                if not ST["mid_exported"]:
                    if await export_named(p0, "mid"):
                        ST["mid_exported"] = True
                        ST["stuck_exported"] = True
                ST["stuck"] = True
                wire("stalled",
                     {"type": prog_key[0], "player": prog_key[1],
                      "rev": prog_key[2]})
                break
        else:
            ST["last_prog_key"] = prog_key
            ST["last_prog_t"] = time.time()
        # stuck detection: sacrifice done, no BF pick prompt. The BF-put
        # DigChoice, when offered, arrives in the SAME End step right after
        # the sacrifice. If the game leaves the End step (or 60s/3 turns
        # pass) with no pick, the ability resolved without its
        # put-onto-BF step: the reported bug.
        if ST["sac_done"] and not ST["bf_prompt_seen"] and not ST["stuck"]:
            moved_on = (turn_now > ST["sac_done_turn"]
                        or (turn_now == ST["sac_done_turn"]
                            and state_now.get("phase") not in ("End",)))
            if (time.time() - ST.get("sac_done_at", t0) > 60
                    or turn_now > ST["sac_done_turn"] + 3
                    or moved_on):
                ST["stuck"] = True
                wire("stuck", {"turn": turn_now,
                               "sac_done_turn": ST["sac_done_turn"]})
                say("[P0] STUCK: sacrifice done, no BF-pick prompt "
                    f"(turn {turn_now})")
                await asyncio.sleep(2.0)
                if not ST["mid_exported"]:
                    if await export_named(p0, "mid"):
                        ST["mid_exported"] = True
                        ST["stuck_exported"] = True
                ST["cleanup_turns"] = 1
        if ST["bf_done"] and not ST["mid_exported"]:
            await asyncio.sleep(2.0)
            if await export_named(p0, "mid"):
                ST["mid_exported"] = True
            ST["cleanup_turns"] = 1
        if ST["mid_exported"]:
            ST["cleanup_turns"] += 1
            if ST["cleanup_turns"] > 60:
                ST["done"] = True
        # safety: ritual never cast by turn 16 -> bail
        if not ST["ritual_on_bf"] and not ST["trigger_fired"] \
                and turn_now >= 16:
            ST["notes"].append("safety: ritual never reached the battlefield; "
                               "bailing")
            break

    if not ST["done"]:
        ST["notes"].append("deadline hit before cleanup completed")
    if not ST["post_exported"]:
        await export_named(p0, "post")
        ST["post_exported"] = True
    await finalize(p0)
    await p0.close()
    await p1.close()


asyncio.run(main())
