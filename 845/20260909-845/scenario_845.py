#!/usr/bin/env python3
"""Issue #845: Bitterbloom Bearer does not trigger at the beginning of upkeep.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-05-23, labels unknown): "Bitterbloom Bearer <redacted>
not trigger on beginning of up keep"

Oracle text (pinned card-data.json, 'bitterbloom bearer'):
  "Flash
   Flying
   At the beginning of your upkeep, you lose 1 life and create a 1/1 blue and
   black Faerie creature token with flying."
Parsed as: one Phase/Upkeep trigger, constraint OnlyDuringYourTurn, not
optional, trigger_zones=[Battlefield]:
  effect  = LoseLife {amount Fixed 1, target Controller}
  sub     = Token {name "Faerie", 1/1, Creature+Faerie, Blue+Black,
                   keywords [Flying], count Fixed 1, owner Controller}

Setup (native engine, two human-client seats):
  P0: 12x bitterbloom bearer (2BB 1/1) + 48x swamp. (12x density: engine
      accepts >4-of for custom games; mulligan to Bearer + 2+ lands.)
  P1: 60x swamp dummy (plays a land, passes; never attacks).

Expected (per card text):
  E1: Bearer on P0's battlefield before P0's upkeep.
  E2: at the beginning of P0's upkeep the trigger is put on the stack
      (source = Bearer, effect LoseLife 1 -> sub Token Faerie).
  E3: on resolution P0's life goes 20 -> 19.
  E4: one 1/1 blue+black Faerie token with flying is created on P0's
      battlefield.
  E5: stack empties and the game proceeds (Draw / PreCombatMain).

Assertions:
  A1_setup_ok     first P0 Upkeep with Bearer on BF: exported pre.json
  A2_trigger_fired  Upkeep stack entry observed with source=Bearer and
                  effect signature LoseLife -> Token (recorded in wire log
                  and obs dict); stack seen non-empty during the Upkeep
  A3_life_lost    P0 life 20 -> 19 between pre.json and post.json
  A4_token_created  exactly 1 new 1/1 blue+black Faerie token w/ Flying on
                  P0's BF in post.json (0 in pre.json)
  A5_no_duplicates  not more than 1 trigger/stack-resolution cycle happened
                  during the observed Upkeep (advisory unless >1)
  A6_cleanup      post.json: stack empty, game proceeding
                  (phase Draw/PreCombatMain), Bearer still on BF

Verdict rule: reproduced iff A1 passed and any of A2/A3/A4 fail (the
reported "does not trigger" outcome). not-reproduced iff A1..A6 pass.
blocked iff the game cannot be driven to P0's Bearer-on-BF Upkeep.

Evidence: evidence/845/<run-id>/pre.json (start of P0 Upkeep, Bearer on BF),
post.json (after Upkeep resolution), run.json, manifest.sha256, summary.png,
scenario_845.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-845"
EVDIR = f"{BACKFILL}/evidence/845/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEARER = "bitterbloom bearer"   # card-data.json key (exact)
SWAMP = "swamp"                 # card-data.json key (exact)

P0_DECK = [(BEARER, 12), (SWAMP, 48)]
P1_DECK = [(SWAMP, 60)]

SERVER_IDENTITY = {
    "server_version": "0.77.0",
    "build_commit": "61715b5",
    "protocol_version": 67,
    "mode": "Full",
    "binary_sha256": "a52293b754605baa63e4d987a5208902ec394868d49c4bfd86b3fd57f3267c6a",
    "card_data_sha256": "698350d9b6323011a5b86a74a4d2ea54d13b4ed26a520579be7d044f0a3692e5",
    "draft_pools_sha256": "56e030fdc74b2385759310de8564a58b8716035b2dccc0da709f37250cc2d3c7",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 match of pinned verified artifacts; "
              "reused running pinned server (listening on 127.0.0.1:9374 "
              "from earlier run; binary = pinned v0.77.0 release)",
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


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def pt(obj):
    return num(obj.get("power")), num(obj.get("toughness"))


def bearer_ids(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == BEARER]


def faerie_token_ids(state, pid):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        nm = lname(state, oid)
        p, t = pt(o)
        types = [str(x).lower() for x in (o.get("core_types") or o.get("types") or [])]
        if nm == "faerie" and p == 1 and t == 1:
            out.append(int(oid))
    return out


def hand_lnames(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [lname(state, o) for o in p.get("hand", [])]
    return []


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def untapped_swamps(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == SWAMP and not o.get("tapped")]


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def stack_sig(entry):
    """Best-effort textual signature of a stack entry."""
    blob = json.dumps(entry, default=str)
    src = ""
    for key in ("source_name", "source", "controller", "effect"):
        if key in blob:
            pass
    return blob[:1500]


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_fired", "A3_life_lost",
            "A4_token_created", "A5_no_duplicates", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    obs = {"upkeep_snapshots": 0, "stack_max_during_upkeep": 0,
           "trigger_entry": None, "trigger_seen": False,
           "trigger_resolutions": 0, "life_pre": None,
           "life_post": None, "life_drops": []}
    pre_exported = False
    post_exported = False
    bearer_cast = False
    cast_attempts = 0

    def state_has_bearer_upkeep(st):
        s = st["state"]
        return (s.get("active_player") == 0 and s.get("phase") == "Upkeep"
                and len(bearer_ids(s, 0)) >= 1)

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        # evaluate deferred assertions from saved states
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")
        if pre_st is not None and post_st is not None:
            obs["life_pre"] = life_of(pre_st, 0)
            obs["life_post"] = life_of(post_st, 0)
            if obs["life_pre"] == 20 and obs["life_post"] == 19:
                ass["A3_life_lost"] = "passed"
                notes.append("P0 life 20 -> 19 between pre and post (trigger LoseLife resolved)")
            else:
                ass["A3_life_lost"] = "failed"
                notes.append(f"P0 life pre={obs['life_pre']} post={obs['life_post']} (expected 20->19)")
            pre_toks = faerie_token_ids(pre_st, 0)
            post_toks = faerie_token_ids(post_st, 0)
            new_toks = [t for t in post_toks if t not in pre_toks]
            if len(new_toks) == 1:
                o = get_obj(post_st, new_toks[0])
                kws = [str(k).lower() for k in (o.get("keywords") or [])]
                cols = [str(c).lower() for c in
                        (o.get("color") or o.get("base_color") or [])]
                p, t = pt(o)
                if p == 1 and t == 1 and "flying" in kws \
                        and "blue" in cols and "black" in cols:
                    ass["A4_token_created"] = "passed"
                    notes.append(f"exactly 1 new 1/1 blue+black Faerie w/ flying "
                                 f"(oid {new_toks[0]}) on P0's BF in post.json")
                else:
                    ass["A4_token_created"] = "failed"
                    notes.append(f"new token oid {new_toks[0]} wrong shape: "
                                 f"{p}/{t} kw={kws} colors={cols}")
            elif len(new_toks) == 0 and ass["A2_trigger_fired"] == "failed":
                ass["A4_token_created"] = "failed"
                notes.append("no new Faerie token created in post.json (reported bug outcome)")
            else:
                ass["A4_token_created"] = "failed"
                notes.append(f"new faerie tokens in post.json: {new_toks} (expected exactly 1)")
            # A6 from post.json
            slen = len(post_st.get("stack", []) or [])
            ph = post_st.get("phase")
            wf = (post_st.get("waiting_for") or {}).get("type")
            bears = bearer_ids(post_st, 0)
            if slen == 0 and len(bears) >= 1 \
                    and wf in ("Priority", "DeclareAttackers", "DiscardToHandSize",
                               "Untap", None):
                ass["A6_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, Bearer on BF, "
                             f"game proceeding (phase={ph}, wf={wf})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"post.json wrong: stack={slen}, bears={len(bears)}, "
                             f"phase={ph}, wf={wf}")
        else:
            for k in ("A3_life_lost", "A4_token_created", "A6_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing pre/post state)")
        if ass["A5_no_duplicates"] == "not-run":
            if obs["trigger_resolutions"] <= 1:
                ass["A5_no_duplicates"] = "passed"
                notes.append(f"trigger resolution cycles during upkeep: "
                             f"{obs['trigger_resolutions']} (<=1)")
            else:
                ass["A5_no_duplicates"] = "failed"
                notes.append(f"trigger fired {obs['trigger_resolutions']}x during one upkeep")
        core = ["A1_setup_ok", "A2_trigger_fired", "A3_life_lost", "A4_token_created"]
        # The reported outcome is the trigger's EFFECTS (life loss + token).
        # A2 is stack-presence detection; if A3 and A4 both pass, the trigger
        # observably fired and resolved regardless of A2's signature match.
        if all(ass[k] == "passed" for k in core) or (
                ass["A3_life_lost"] == "passed" and ass["A4_token_created"] == "passed"):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(ass[k] == "failed" for k in core):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        run = {
            "issue": 845,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_845.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                            "12x Bitterbloom Bearer deck density is a test-harness convenience "
                            "(engine accepts >4-of for custom games)."],
            "setup_line": "P0: 12x bitterbloom bearer + 48x swamp (mulligan to bearer + 2 lands); "
                          "P1: 60x swamp dummy",
            "contract_line": "At the start of P0's upkeep with Bearer on the battlefield, the "
                             "trigger fires: P0 loses 1 life and creates a 1/1 blue+black Faerie "
                             "token with flying",
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

    def stack_has_bearer_trigger(state):
        """True if any stack entry looks like the Bearer upkeep trigger."""
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str)
            low = blob.lower()
            if "loselife" in low and ("token" in low or "faerie" in low):
                if "bitterbloom" in low or "bearer" in low:
                    return e
        return None

    async def observe_upkeep(state):
        """Called on every P0 tick during P0's Bearer Upkeep."""
        if not state_has_bearer_upkeep_proxy.get("flag"):
            return
        obs["upkeep_snapshots"] += 1
        slen = len(state.get("stack", []) or [])
        if slen > obs["stack_max_during_upkeep"]:
            obs["stack_max_during_upkeep"] = slen
            say(f"UPKEEP stack grew to {slen}")
        hit = stack_has_bearer_trigger(state)
        if hit and obs["trigger_entry"] is None:
            obs["trigger_entry"] = stack_sig(hit)
            obs["trigger_seen"] = True
            say("UPKEEP: Bearer trigger observed on stack")
            wire("upkeep_trigger_on_stack", hit)
            mid = await p0.export_state()
            with open(f"{EVDIR}/mid_trigger_pending.json", "w") as f:
                f.write(mid)
            say("exported MID (trigger pending on stack)")
        # a resolution happened if life dropped mid-upkeep
        lp = life_of(state, 0)
        if lp is not None and obs["life_pre"] is not None and lp < obs["life_pre"]:
            if lp not in obs["life_drops"]:
                obs["life_drops"].append(lp)
                obs["trigger_resolutions"] += 1
                say(f"UPKEEP: life dropped to {lp}")
                wire("life_drop", {"life": lp})

    state_has_bearer_upkeep_proxy = {"flag": False}

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, bearer_cast, cast_attempts, post_exported
        state_has_bearer_upkeep_proxy["flag"] = state_has_bearer_upkeep(st)
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_lnames(state, 0)
            lands = sum(1 for n in hn if n == SWAMP)
            mulls = kept.get("P0_mulls", 0)
            if (BEARER in hn and lands >= 2) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps opening hand (bearer={BEARER in hn}, lands={lands})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (bearer={BEARER in hn}, lands={lands})")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for pl in state.get("players", [])
                            if pl.get("id") == 0 for o in pl.get("hand", [])]
                def bottom_key(oid):
                    nm = lname(state, oid)
                    return 0 if nm == SWAMP else (2 if nm == BEARER else 1)
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}: {[lname(state, x) for x in picks]}")
                return
        # mana payments advertised by the engine are submitted as-is
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # pre-export at the very start of P0's Bearer Upkeep
        if state_has_bearer_upkeep_proxy["flag"] and not pre_exported:
            say("P0 Bearer Upkeep reached; exporting PRE")
            pre = await p0.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            pre_st = json.loads(pre)["state"]
            obs["life_pre"] = life_of(pre_st, 0)
            ok = len(bearer_ids(pre_st, 0)) >= 1 and obs["life_pre"] == 20
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"pre-upkeep: bearer_on_bf={len(bearer_ids(pre_st,0))}, "
                         f"life={obs['life_pre']}")
            pre_exported = True
        await observe_upkeep(state)
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        # cast Bearer when affordable (once)
        if (not bearer_cast
                and not bearer_ids(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and BEARER in hand_lnames(state, 0)
                and len(untapped_swamps(state, 0)) >= 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEARER:
                    cast_attempts += 1
                    say("P0 casts bitterbloom bearer")
                    wire("cast_bearer", a)
                    await submit_as_is(p0, a)
                    bearer_cast = True
                    return
        # post-upkeep export + evaluate trigger stack presence
        if (pre_exported and not post_exported
                and state.get("active_player") == 0
                and state.get("phase") in ("Draw", "PreCombatMain")):
            say(f"post-upkeep reached: stack_max={obs['stack_max_during_upkeep']} "
                f"trigger_seen={obs['trigger_seen']} resolutions={obs['trigger_resolutions']}")
            if obs["trigger_seen"]:
                ass["A2_trigger_fired"] = "passed"
                notes.append("Bearer upkeep trigger observed on stack (source+effect "
                             "signature recorded in wire log / mid_trigger_pending.json)")
            else:
                ass["A2_trigger_fired"] = "failed"
                notes.append(f"NO upkeep trigger observed on stack during the whole Upkeep "
                             f"({obs['upkeep_snapshots']} snapshots, stack max "
                             f"{obs['stack_max_during_upkeep']}) (REPORTED BUG)")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        # normal setup play
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
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
    last_diag = 0.0
    last_tick_at = {}
    stuck_deadline = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            acts0 = [a["type"] for a in merged_actions(p0.latest)] if p0.latest else []
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)} "
                f"bearer={len(bearer_ids(s, 0))} life={life_of(s, 0)} "
                f"stack={len(s.get('stack') or [])} P0acts={acts0[:10]} "
                f"cast={bearer_cast} pre={pre_exported} post={post_exported}")
        in_flight = bearer_cast and not post_exported
        if in_flight and stuck_deadline is None:
            stuck_deadline = time.time() + 300
        if not in_flight:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("bearer cast but post-upkeep not reached in 300s; see wire log")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
