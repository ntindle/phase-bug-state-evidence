#!/usr/bin/env python3
"""Issue #7178: Charismatic Conqueror -- makes the token and does not give
players the option to tap their creatures upon entering the battlefield.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.83.0):
  Charismatic Conqueror ({1}{W}, 2/2 Creature - Vampire Soldier):
    "Vigilance
     Whenever an artifact or creature an opponent controls enters untapped,
     they may tap that permanent. If they don't, you create a 1/1 white
     Vampire creature token with lifelink."

Card-data parse state on v0.83.0 (verified 2026-09-15 before the run):
  triggers[0] = ChangesZone, valid_card = Or(Typed[Artifact] controller
    Opponent, Typed[Creature] controller Opponent), destination Battlefield,
    optional=true, optional_player=TriggeringPlayer,
    execute = kind Spell, effect SetTapState(Tap, target TriggeringSource,
    scope Single), description "they may tap that permanent",
    sub_ability = kind Spell, effect Token(Vampire 1/1 white lifelink,
    owner Controller), condition Not(EffectOutcome OptionalEffectPerformed).

  The parser emits the correct shape: the may-tap is optional with the
  TRIGGERING PLAYER (the opponent whose permanent entered) as the decider,
  and the token is gated on Not(OptionalEffectPerformed). The reported
  defect is therefore runtime, not parse-level.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Charismatic Conqueror, 48x Plains.
  P1: 12x Grizzly Bears, 48x Forest (passive except land drops + 2 Bears).

Planned line:
  P0 turn 2: pre.json export, cast Charismatic Conqueror.
  P1 turn 2: cast Grizzly Bears #1 (enters untapped) -> trigger window A.
    Expected: an OptionalEffectChoice offered to P1 (the triggering player).
    Leg A answers accept=true -> Bears tapped, NO token on P0's side.
  P1 turn 3: cast Grizzly Bears #2 -> trigger window B.
    Leg B answers accept=false -> Bears untapped, ONE 1/1 white Vampire
    lifelink token on P0's side.
  Cleanup: export post.json after both legs settle, stack empty, game
    proceeds.

The reported symptom: the token is created and NO option is ever offered.
If the engine reproduces that shape, no OptionalEffectChoice will appear
for either window and a Vampire token will be on P0's battlefield without
any choice having been recorded.

Assertions (each passed / failed / not-run):
  A1_parse          card-data: trigger optional=true, optional_player=
                    TriggeringPlayer, main SetTapState(Tap,TriggeringSource),
                    sub Token gated on Not(OptionalEffectPerformed).
  A2_setup          PRE: Charismatic Conqueror in P0 hand (pre-cast);
                    both Grizzly Bears legally cast and entered.
  A3_option_offered the may-tap OptionalEffectChoice was offered to P1
                    (the triggering player) at least once.
  A4_accept_leg     leg A answered accept=true: Bears #1 tapped, zero
                    Vampire tokens on P0 BF. (not-run if no prompt.)
  A5_decline_leg    leg B answered accept=false: Bears #2 untapped, exactly
                    one 1/1 Vampire lifelink token on P0 BF. (not-run if no
                    prompt.)
  A6_cleanup        stack empty, game proceeded past both legs.

Verdict rule:
  blocked        iff A2 fails (setup never reached).
  reproduced     iff A2 passes and A3 fails -- no may-tap option was ever
                 offered while the trigger fired (the reported symptom),
                 or a leg misbehaves after a choice is answered.
  not-reproduced iff A2/A3 pass and both legs behave correctly.

Evidence: evidence/7178/<run-id>/pre.json, mid_accept.json, mid_decline.json,
post.json, run.json, manifest.sha256, summary.png, scenario_7178.py,
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
ISSUE = 7178
RUN_ID = os.environ.get("RUN_ID", "20260915-7178")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CONQUEROR = "charismatic conqueror"
BEARS = "grizzly bears"
PLAINS = "plains"
FOREST = "forest"

P0_DECK = [(CONQUEROR, 12), (PLAINS, 48)]
P1_DECK = [(BEARS, 12), (FOREST, 48)]

SERVER_IDENTITY = {
    "server_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key); server already running "
              "on 127.0.0.1:9374 by this run's session.",
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


def untapped_lands(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names):
            if not o.get("tapped"):
                out.append(int(oid))
    return out


def is_vampire_token(state, oid):
    o = get_obj(state, oid)
    if o.get("zone") != "Battlefield" or o.get("controller") != 0:
        return False
    nm = str(o.get("base_name") or o.get("name") or "").lower()
    if "vampire" not in nm:
        return False
    return True


def vampire_token_oids(state):
    return [int(oid) for oid in (state.get("objects", {}) or {})
            if is_vampire_token(state, oid)]


def token_stats(state, oid):
    o = get_obj(state, oid)
    kws = o.get("keywords") or []
    kw_names = []
    for k in kws:
        if isinstance(k, str):
            kw_names.append(k.lower())
        elif isinstance(k, dict):
            kw_names.append(str(k.get("name") or k.get("keyword")
                                or json.dumps(k))[:40].lower())
    return {"power": o.get("power"), "toughness": o.get("toughness"),
            "tapped": o.get("tapped"), "keywords": kw_names,
            "is_token": o.get("is_token")}


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


def answer_vi_choice(c, opp, choice):
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
    return sub


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",          # setup -> conqueror_pending ->
                                   # legA -> legB -> cleanup -> done
        "pre_exported": False,
        "post_exported": False,
        "conqueror_cast": False,
        "conqueror_entered": False,
        "conqueror_oid": None,
        "bears_cast_count": 0,
        "bears1_oid": None,
        "bears2_oid": None,
        "legA_open": False, "legA_answered": False, "legA_accept": None,
        "legA_prompt_seen": False, "legA_mid_exported": False,
        "legA_opened_at": None,
        "legB_open": False, "legB_answered": False, "legB_accept": None,
        "legB_prompt_seen": False, "legB_mid_exported": False,
        "legB_opened_at": None,
        "answered_iids": [],
        "optional_seen": [],       # records of every OptionalEffectChoice
        "wf_types_seen": [],
        "trigger_stack_seen": False,  # Conqueror trigger observed on stack
        "turns_seen": set(),
        "cleanup_from_turn": None,
        "game_code": None,
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-conqueror")
    p1 = PhaseClient("P1-bears")
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
            say(f"exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

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

    async def handle_discard(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            # discard filler lands first, never the key cards
            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if CONQUEROR in tx or BEARS in tx:
                    return 2
                return 0
            pick = sorted(chs, key=rank)[0]
            if acted(f"disc{iid}", st.get("state_revision", -1)):
                return True
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(answer_vi_choice(c, opp, pick))
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

    def find_accept_choice(opp, accept):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                role = str(d.get("role", "")).lower()
                val = str(d.get("value", "")).lower()
                if role == "accept" and val == ("true" if accept else "false"):
                    return ch
        return None

    async def answer_optional(c, opp, accept, tag, leg):
        """Answer an OptionalEffectChoice-style opportunity via value
        surfaces; returns True if a choice was submitted."""
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        wire("optional_choices",
             {"tag": tag, "leg": leg, "iid": str(iid)[:16],
              "accept": accept,
              "choices": [{"id": ch.get("id"),
                           "text": str(ch.get("text"))[:80],
                           "surfaces": ch.get("surfaces")}
                          for ch in chs]})
        pick = find_accept_choice(opp, accept)
        if pick is None:
            wire("optional_no_accept_surface",
                 {"tag": tag, "leg": leg, "iid": str(iid)[:16]})
            say(f"[{tag}] {leg}: no accept={accept} surface found, "
                "leaving prompt unanswered")
            return False
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick["id"]}}}
        wire("optional_answer", {"tag": tag, "leg": leg, "accept": accept,
                                 "choice_id": pick.get("id"),
                                 "submission": sub})
        await c.send_interaction(sub)
        say(f"[{tag}] {leg} may-tap answered accept={accept}")
        return True

    async def handle_may_tap(c, pid, tag, st, state):
        """OptionalEffectChoice for the Conqueror may-tap, offered to the
        triggering player (P1). Leg A answers accept=true, leg B answers
        accept=false. Records every sighting."""
        wtype = wf_of(state).get("type") or ""
        if wtype != "OptionalEffectChoice":
            return False
        if wf_player(state) != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            rec = {"iid": str(iid)[:16], "player": pid,
                   "leg": "A" if ST["legA_open"] and not ST["legA_answered"]
                          else ("B" if ST["legB_open"] and not ST["legB_answered"]
                                else "?")}
            if rec not in ST["optional_seen"]:
                ST["optional_seen"].append(rec)
            wire("optional_seen", {"tag": tag, **rec})
            if ST["legA_open"] and not ST["legA_answered"]:
                ST["legA_prompt_seen"] = True
                if await answer_optional(c, opp, True, tag, "legA"):
                    ST["answered_iids"].append(iid)
                    ST["legA_answered"] = True
                    ST["legA_accept"] = True
                return True
            if ST["legB_open"] and not ST["legB_answered"]:
                ST["legB_prompt_seen"] = True
                if await answer_optional(c, opp, False, tag, "legB"):
                    ST["answered_iids"].append(iid)
                    ST["legB_answered"] = True
                    ST["legB_accept"] = False
                return True
            # an optional prompt outside the two legs: record, do not drive
            say(f"[{tag}] unexpected OptionalEffectChoice outside legs, "
                "leaving unanswered")
            ST["leg_outside_prompt"] = True
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

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")
        ST["turns_seen"].add(turn)

        # track Conqueror entering
        cq_bf = bf_ids(state, 0, CONQUEROR)
        if cq_bf and not ST["conqueror_entered"]:
            ST["conqueror_entered"] = True
            ST["conqueror_oid"] = cq_bf[0]
            say(f"[P0] Charismatic Conqueror ENTERED oid={cq_bf[0]} "
                f"turn={turn} phase={phase}")
            ST["stage"] = "legA"

        # track Bears entering (open leg windows)
        bears_bf = bf_ids(state, 1, BEARS)
        for oid in bears_bf:
            if ST["bears1_oid"] is None and oid not in (ST["bears1_oid"],):
                ST["bears1_oid"] = oid
                ST["legA_open"] = True
                ST["legA_opened_at"] = time.time()
                say(f"[P0] Bears #1 ENTERED oid={oid} -> legA window open "
                    f"(turn={turn} phase={phase})")
            elif (ST["bears1_oid"] is not None and ST["bears2_oid"] is None
                    and oid != ST["bears1_oid"]):
                ST["bears2_oid"] = oid
                ST["legB_open"] = True
                ST["legB_opened_at"] = time.time()
                say(f"[P0] Bears #2 ENTERED oid={oid} -> legB window open "
                    f"(turn={turn} phase={phase})")

        # track the Conqueror trigger hitting the stack (kind +
        # ability description discriminate it from other triggers)
        if ST["conqueror_oid"] is not None and not ST["trigger_stack_seen"]:
            for e in (state.get("stack") or []):
                kind = (e.get("kind") or {})
                if kind.get("type") == "TriggeredAbility" \
                        and e.get("source_id") == ST["conqueror_oid"]:
                    ab = (e.get("ability") or {})
                    if "may tap" in str(ab.get("description") or "").lower():
                        ST["trigger_stack_seen"] = True
                        wire("trigger_on_stack",
                             {"source_id": e.get("source_id"),
                              "id": e.get("id")})
                        say("[P0] Conqueror may-tap trigger ON STACK")
                        break

        # leg window close: answered + settled, or 25s timeout with empty stack
        def close_leg(prefix, mid_name):
            open_k = f"{prefix}_open"
            ans_k = f"{prefix}_answered"
            exp_k = f"{prefix}_mid_exported"
            t_k = f"{prefix}_opened_at"
            if ST[open_k] and not ST[exp_k]:
                settled = not (state.get("stack") or [])
                timed_out = (time.time() - (ST[t_k] or 0)) > 25
                if (ST[ans_k] and settled) or (timed_out and settled):
                    return True
            return False

        if close_leg("legA", "mid_accept"):
            if await export_named("mid_accept"):
                ST["legA_mid_exported"] = True
                say(f"[P0] legA closed: answered={ST['legA_answered']} "
                    f"prompt_seen={ST['legA_prompt_seen']} "
                    f"tokens={len(vampire_token_oids(state))}")
        if close_leg("legB", "mid_decline"):
            if await export_named("mid_decline"):
                ST["legB_mid_exported"] = True
                say(f"[P0] legB closed: answered={ST['legB_answered']} "
                    f"prompt_seen={ST['legB_prompt_seen']} "
                    f"tokens={len(vampire_token_oids(state))}")
                ST["stage"] = "cleanup"
                ST["cleanup_from_turn"] = turn

        if ST["stage"] == "cleanup":
            if turn > ST.get("cleanup_from_turn", turn) \
                    and not (state.get("stack") or []):
                if await export_named("post"):
                    ST["post_exported"] = True
                    ST["stage"] = "done"
                return

        # zero-attacker combat for P0 too (cf. legA stall: P0's own
        # DeclareAttackers waits otherwise; p0_tick previously had no
        # combat handler)
        if await combat_empty(p0, 0, "P0", st, state, acts):
            return
        # record (and if needed answer) a may-tap choice misdirected to P0
        if await handle_may_tap(p0, 0, "P0", st, state):
            return

        # --- main-phase actions ---
        in_flight = ST["conqueror_cast"] and not ST["conqueror_entered"] \
            and any(CONQUEROR in str(
                e.get("name") or e.get("card_name") or "").lower()
                    for e in (state.get("stack") or []))
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain") \
                and not in_flight:
            # land drop (retry every tick; no kept flag)
            for oid in hand_ids(state, 0):
                if lname(state, oid) == PLAINS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            # cast Conqueror when mana is ready
            cq_hand = any(n == CONQUEROR for n in hand_lnames(state, 0))
            ul = untapped_lands(state, 0, (PLAINS,))
            if (not ST["conqueror_cast"] and cq_hand and len(ul) >= 2
                    and turn >= 2):
                ca = cast_spell_action(acts, state, 0, CONQUEROR)
                if ca and not acted("conqueror", rev):
                    if not ST["pre_exported"]:
                        if await export_named("pre"):
                            ST["pre_exported"] = True
                    say("[P0] casting Charismatic Conqueror")
                    await submit_as_is(p0, ca)
                    ST["conqueror_cast"] = True
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
        if await handle_discard(p1, 1, "P1", st, state):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        # answer the may-tap choice (leg A accept, leg B decline)
        if await handle_may_tap(p1, 1, "P1", st, state):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
            # cast Bears #1 once the Conqueror is out (leg A)
            n_bears_hand = sum(1 for n in hand_lnames(state, 1)
                               if n == BEARS)
            ul = untapped_lands(state, 1, (FOREST,))
            if (ST["conqueror_entered"] and ST["bears_cast_count"] == 0
                    and n_bears_hand >= 1 and len(ul) >= 2 and turn >= 2):
                ca = cast_spell_action(acts, state, 1, BEARS)
                if ca and not acted("bears1", rev):
                    say("[P1] casting Grizzly Bears #1 (leg A: accept "
                        "branch)")
                    await submit_as_is(p1, ca)
                    ST["bears_cast_count"] = 1
                    return
            # cast Bears #2 only after leg A settled (leg B: decline branch)
            if (ST["legA_mid_exported"] and ST["bears_cast_count"] == 1
                    and n_bears_hand >= 1 and len(ul) >= 2):
                ca = cast_spell_action(acts, state, 1, BEARS)
                if ca and not acted("bears2", rev):
                    say("[P1] casting Grizzly Bears #2 (leg B: decline "
                        "branch)")
                    await submit_as_is(p1, ca)
                    ST["bears_cast_count"] = 2
                    return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
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
        while time.time() - t0 < 1200:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"cq={ST['conqueror_entered']} "
                    f"b1={ST['bears1_oid']} b2={ST['bears2_oid']} "
                    f"promptsA={ST['legA_prompt_seen']} "
                    f"promptsB={ST['legB_prompt_seen']}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "mid_accept", "mid_decline", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre = states.get("pre")
        midA = states.get("mid_accept")
        midB = states.get("mid_decline")
        post = states.get("post")

        def num(x):
            try:
                return int(x)
            except Exception:
                return None

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.83.0/data/card-data.json"))
            c = cd["charismatic conqueror"]
            t0d = (c.get("triggers") or [{}])[0]
            ex = (t0d.get("execute") or {})
            trig_opt = ex.get("optional") is True
            op = ex.get("optional_player") or {}
            trig_player = (op.get("type") == "TriggeringPlayer")
            eff = ex.get("effect") or {}
            main_tap = (eff.get("type") == "SetTapState"
                        and (eff.get("state") or {}).get("type") == "Tap"
                        and (eff.get("target") or {}).get("type")
                        == "TriggeringSource")
            sub = ex.get("sub_ability") or {}
            sub_tok = (sub.get("effect") or {}).get("type") == "Token"
            cond = sub.get("condition") or {}
            cond_not_perf = (cond.get("type") == "Not"
                             and (cond.get("condition") or {}).get("type")
                             == "EffectOutcome"
                             and (cond.get("condition") or {}).get("signal")
                             == "OptionalEffectPerformed")
            ok = trig_opt and trig_player and main_tap and sub_tok \
                and cond_not_perf
            notes.append(f"A1: execute.optional={ex.get('optional')}, "
                         f"optional_player={op}, "
                         f"main_effect={eff.get('type')}, "
                         f"sub_effect={(sub.get('effect') or {}).get('type')}, "
                         f"sub_condition={json.dumps(cond)[:120]}")
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        # pre.json is pre-cast (Conqueror in P0 hand); the Bears casts came
        # later, proven by driver_state (both reached the battlefield).
        if pre is not None:
            cq_hand = sum(1 for n in hand_lnames(pre, 0) if n == CONQUEROR)
            ok = (cq_hand >= 1 and ST["conqueror_entered"]
                  and ST["bears1_oid"] is not None
                  and ST["bears2_oid"] is not None)
            notes.append(f"A2: conqueror_in_p0_hand(pre)={cq_hand} "
                         f"conqueror_entered={ST['conqueror_entered']} "
                         f"bears1_oid={ST['bears1_oid']} "
                         f"bears2_oid={ST['bears2_oid']} "
                         f"(both Bears legally cast and entered)")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: option offered ----
        ok = (ST["legA_prompt_seen"] or ST["legB_prompt_seen"])
        notes.append(f"A3: trigger_on_stack={ST['trigger_stack_seen']}, "
                     f"legA_prompt={ST['legA_prompt_seen']} "
                     f"legB_prompt={ST['legB_prompt_seen']} "
                     f"optional_seen={ST['optional_seen']}")
        ass["A3_option_offered"] = "passed" if ok else "failed"

        # ---- A4: accept leg ----
        if midA is not None and ST["legA_prompt_seen"] \
                and ST["legA_answered"]:
            b1 = ST["bears1_oid"]
            tapped = get_obj(midA, b1).get("tapped") if b1 else None
            toks = vampire_token_oids(midA)
            ok = (tapped is True and len(toks) == 0)
            notes.append(f"A4: bears1 tapped={tapped} (expect True), "
                         f"p0_vampire_tokens={len(toks)} (expect 0)")
            ass["A4_accept_leg"] = "passed" if ok else "failed"
        elif ST["legA_prompt_seen"] and not ST["legA_answered"]:
            ass["A4_accept_leg"] = "not-run"
            notes.append("A4 not-run: prompt seen but no answer submitted")
        else:
            ass["A4_accept_leg"] = "not-run"
            notes.append("A4 not-run: no may-tap prompt was offered "
                         "(legA_mid state records the bug shape instead)")

        # ---- A5: decline leg ----
        if midB is not None and ST["legB_prompt_seen"] \
                and ST["legB_answered"]:
            b2 = ST["bears2_oid"]
            tapped = get_obj(midB, b2).get("tapped") if b2 else None
            toks = vampire_token_oids(midB)
            tok_stats = [token_stats(midB, o) for o in toks]
            pow_ok = all(num(s["power"]) == 1 and num(s["toughness"]) == 1
                         for s in tok_stats)
            ll_ok = all("lifelink" in s["keywords"] for s in tok_stats)
            ok = (tapped is False and len(toks) == 1 and pow_ok and ll_ok)
            notes.append(f"A5: bears2 tapped={tapped} (expect False), "
                         f"p0_vampire_tokens={len(toks)} (expect 1), "
                         f"stats={tok_stats}")
            ass["A5_decline_leg"] = "passed" if ok else "failed"
        elif ST["legB_prompt_seen"] and not ST["legB_answered"]:
            ass["A5_decline_leg"] = "not-run"
            notes.append("A5 not-run: prompt seen but no answer submitted")
        else:
            ass["A5_decline_leg"] = "not-run"
            notes.append("A5 not-run: no may-tap prompt was offered")

        # bug-shape corroboration on the no-prompt path: was a token made
        # without any choice being offered?
        if midA is not None and not ST["legA_prompt_seen"]:
            toksA = vampire_token_oids(midA)
            notes.append(f"bug-shape: legA with NO prompt -> p0 vampire "
                         f"tokens in mid_accept.json = {len(toksA)} "
                         f"(report: token made, no option given)")
        if post is not None and not ST["legA_prompt_seen"] \
                and not ST["legB_prompt_seen"]:
            toksP = vampire_token_oids(post)
            notes.append(f"bug-shape: post.json p0 vampire tokens = "
                         f"{len(toksP)} (no prompt ever offered)")

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty and wft in ("Priority",)
            notes.append(f"A6: stack_empty={stack_empty} post_wf={wft} "
                         f"turns_seen={sorted(ST['turns_seen'])}")
        else:
            ok = False
            notes.append("A6 failed: post.json missing")
        ass["A6_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif not ST["trigger_stack_seen"] \
                and ass["A3_option_offered"] == "failed":
            verdict = "blocked"
            notes.append("verdict=blocked: the Conqueror trigger never "
                         "reached the stack, so the may-tap option's "
                         "absence cannot be attributed to the reported "
                         "defect (trigger firing shape unobserved)")
        elif ass["A3_option_offered"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the Conqueror trigger fired on "
                         "an opponent's entering creature, but the may-tap "
                         "option was NEVER offered to the triggering player "
                         "- the exact reported symptom")
        elif ass["A4_accept_leg"] == "failed" \
                or ass["A5_decline_leg"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the option was offered but a "
                         "leg behaved incorrectly (related failure)")
        elif ass["A4_accept_leg"] == "passed" \
                and ass["A5_decline_leg"] == "passed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: the may-tap option was "
                         "offered and both accept/decline branches behaved "
                         "per Oracle")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.83.0 single-user server on "
                               "127.0.0.1:9374 (started by this run's "
                               "session under runs/20260915-7178/)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid_accept.json",
                               "mid_decline.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is near-passive (lands + two Grizzly Bears, never "
                "attacks/blocks) so the trigger windows stay clean.",
                "Only creatures (Grizzly Bears) exercised the trigger; the "
                "artifact half of the clause was not separately tested.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{srv_run}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
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
        W, H = 1000, 1080
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7178 - Charismatic Conqueror",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-15 - "
               "may-tap option on opponent's entering creature",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: 'Whenever an artifact or creature an "
               "opponent controls enters",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "untapped, they may tap that permanent. If they "
               "don't, you create a",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "1/1 white Vampire creature token with lifelink.'",
               fill=(200, 210, 225))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: optional may-tap, TriggeringPlayer "
                        "decides; token gated on Not(OptionalEffectPerformed)",
            "A2_setup": "PRE: Conqueror on P0 BF, Bears in P1 hand, "
                        "P1 mana ready",
            "A3_option_offered": "may-tap OptionalEffectChoice offered to "
                                 "the triggering player (P1)",
            "A4_accept_leg": "accept -> Bears tapped, no token",
            "A5_decline_leg": "decline -> Bears untapped, 1/1 Vampire "
                              "lifelink token",
            "A6_cleanup": "stack empty, game proceeded",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120),
                   "failed": (255, 90, 90),
                   "not-run": (230, 200, 120)}.get(v, (180, 180, 180))
            d.text((40, y), f"{v:>9}", fill=col)
            d.text((130, y), lab, fill=(210, 220, 235))
            y += 24
        y += 8

        def life(s, pid):
            for p in (s or {}).get("players", []):
                if p.get("id") == pid:
                    return p.get("life")
            return "?"

        def mid_block(label, s, bears_oid):
            nonlocal y
            d.text((24, y), label, fill=(200, 210, 225))
            y += 24
            if s is None:
                d.text((40, y), "state MISSING", fill=(255, 90, 90))
            else:
                bt = get_obj(s, bears_oid).get("tapped") \
                    if bears_oid else "?"
                ntok = len(vampire_token_oids(s))
                d.text((40, y), f"Bears tapped={bt}; P0 Vampire tokens="
                       f"{ntok}; life={life(s, 0)}/{life(s, 1)}",
                       fill=(180, 195, 215))
            y += 30

        midA, midB, post = (states.get("mid_accept"),
                            states.get("mid_decline"), states.get("post"))
        ds = run.get("driver_state", {}) or {}
        mid_block("Leg A (accept=true -> expect Bears tapped, 0 tokens):",
                  midA, ds.get("bears1_oid"))
        mid_block("Leg B (decline -> expect Bears untapped, 1 token):",
                  midB, ds.get("bears2_oid"))
        d.text((24, y), "Post:", fill=(200, 210, 225))
        y += 24
        if post is not None:
            d.text((40, y), f"life={life(post, 0)}/{life(post, 1)}; "
                   f"stack={len(post.get('stack') or [])}; "
                   f"P0 tokens={len(vampire_token_oids(post))}",
                   fill=(180, 195, 215))
        else:
            d.text((40, y), "post.json MISSING", fill=(255, 90, 90))
        y += 40
        d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
               f"{ISSUE}/" + RUN_ID + "/", fill=(140, 160, 180))
        p = os.path.join(EVDIR, "summary.png")
        img.save(p)
        say(f"saved summary.png ({os.path.getsize(p)} bytes)")

    def write_manifest():
        files = [f for f in sorted(os.listdir(EVDIR))
                 if f not in ("manifest.sha256",)]
        lines = []
        for fn in files:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
            f.write("\n".join(lines) + "\n")
        say(f"wrote manifest.sha256 ({len(lines)} files)")

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
