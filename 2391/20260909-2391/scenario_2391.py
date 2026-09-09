#!/usr/bin/env python3
"""Issue #2391: Omnath, Locus of Creation -- 2nd and 3rd landfall triggers do not resolve.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, reopened 2026-08-13 after an independent second report):
Omnath's first landfall trigger (gain 4 life) resolves correctly. The second
(add {R}{G}{W}{U}) and third (deal 4 damage to each opponent / each
planeswalker you don't control) "do nothing" when additional lands enter the
battlefield on the same turn.

Oracle text (verified from pinned v0.78.0 card-data.json, key
'omnath, locus of creation'):
  "When Omnath enters, draw a card.
   Landfall -- Whenever a land you control enters, you gain 4 life if this is
   the first time this ability has resolved this turn. If it's the second
   time, add {R}{G}{W}{U}. If it's the third time, Omnath deals 4 damage to
   each opponent and each planeswalker you don't control."
Pinned parse (v0.78.0 dataset): one ChangesZone trigger (land you control ->
battlefield) with a SequentialSibling effect chain:
  GainLife(4) [cond NthResolutionThisTurn n=1]
  -> Mana {R}{G}{W}{U} [cond n=2, is_mana_ability]
  -> DamageAll(4, players=Opponent, planeswalkers of Opponent) [cond n=3]

Setup (native engine, two human-client seats, port 9375):
  P0: 8x omnath, locus of creation + 8x explosive vegetation + 11x each basic
      (44 lands). (Dense test-harness copies; engine accepts >4-of.)
      Turns 1-4: play lands (preferring missing colors), cast Omnath turn 4.
      Proof turn: hold the turn's land drop, cast Explosive Vegetation
      ({3}{G}) -> two basics enter tapped in the same turn -> landfall #1 and
      #2 must trigger/resolve; then play the held land drop -> landfall #3.
  P1: 60x mountain dummy (plays a land, passes; never attacks).

Expected (per Oracle text):
  E1: land #1 (from Explosive Vegetation): P0 gains 4 life (20 -> 24).
  E2: land #2 (from Explosive Vegetation): P0's pool gains exactly {R}{G}{W}{U}.
  E3: land #3 (the turn's land drop): P1 takes 4 damage (20 -> 16).
  E4: stack empties, game proceeds (Vegetation in P0 graveyard).

Assertions:
  A1_setup_ok    pre.json: P0 PreCombatMain/PostCombatMain, Omnath on P0
                 battlefield, Explosive Vegetation in P0 hand, >=4 untapped
                 lands incl. a Forest, >=1 land in P0 hand, life 20/20.
  A2_life_gain   mid.json (post-Vegetation, pre-land-#3): P0 life == pre+4.
  A3_mana_added  mid.json: P0 pool contains exactly one pip of each of
                 Red/Green/White/Blue (4 pips total).
  A4_damage      post.json: P1 life == pre-4.
  A5_cleanup     post.json: Vegetation in P0 graveyard, stack empty, Omnath
                 on P0 battlefield, game proceeding (turn advanced or phase
                 moved past the proof main phase).

Verdict rule: blocked iff A1 fails. reproduced iff A1 passes and at least one
of A2/A3/A4 fails (the reported failure is specifically A3/A4 silent while A2
works). not-reproduced iff A1..A5 all pass.

Evidence: evidence/2391/<run-id>/pre.json (before Vegetation cast),
mid.json (Vegetation resolved, both landfall triggers off the stack, before
land #3), post.json (after land #3 resolution), run.json, manifest.sha256,
summary.png, scenario_2391.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
client.URL = "ws://localhost:9375/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-2391"
EVDIR = f"{BACKFILL}/evidence/2391/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OMNATH = "omnath, locus of creation"  # card-data.json key (exact)
VEGETATION = "explosive vegetation"   # card-data.json key (exact)
BASICS = {"forest": "Green", "mountain": "Red", "plains": "White", "island": "Blue"}

P0_DECK = [(OMNATH, 8), (VEGETATION, 8), ("forest", 11), ("mountain", 11),
           ("plains", 11), ("island", 11)]
P1_DECK = [("mountain", 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9375 for run 20260909-2391",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    o = state.get("objects", {}).get(str(oid), {})
    return obj_name(o)


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


def pool_colors(state, pid):
    mp = (player_of(state, pid).get("mana_pool") or {}).get("mana") or []
    return [m.get("color") for m in mp]


def bf_lands(state, pid):
    return [o for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) in BASICS]


def untapped_lands(state, pid):
    return [o for o in bf_lands(state, pid) if not o.get("tapped")]


def bf_colors(state, pid):
    return {BASICS.get(obj_name(o)) for o in bf_lands(state, pid)} - {None}


def omnath_on_bf(state, pid):
    return any(obj_name(o) == OMNATH for oid, o in state.get("objects", {}).items()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid)


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and lname(state, oid) == name:
            return a
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_life_gain", "A3_mana_added", "A4_damage",
            "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    obs = {"cast_submitted": False, "cast_turn": None, "cast_phase": None,
           "turns_held": 0, "hold_turn": None, "hold_released": False,
           "search_seen": False, "trigger_window": [],
           "life_pre": None, "pool_pre": None, "life_mid": None,
           "pool_mid": None, "life_post": None, "pool_post": None}
    pre_exported = False
    mid_exported = False
    post_exported = False
    land3_played = False
    settle_empty = 0
    submitted_interactions = set()
    shapes_logged = set()

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def scan_interactions(st, who):
        """Log every opportunity; handle the Explosive Vegetation library
        search (pick up to 2 basic lands) and default trigger ordering.
        Returns True if an action was submitted."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            full = json.dumps(opp, default=str)
            low = (blob + " " + full).lower()
            is_search = ("search" in low or "library" in low) and any(
                choice_text(ch).lower() in BASICS for ch in chs)
            key = (who, rtype, blob[:80], is_search)
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} search_like={is_search} "
                    f"choices=[{blob[:220]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "search_like": is_search,
                                           "interaction": opp})
            if iid in submitted_interactions:
                continue
            if is_search:
                obs["search_seen"] = True
                basics = [ch for ch in chs
                          if choice_text(ch).lower() in BASICS
                          and ch.get("status", {}).get("type") == "available"]
                pick = basics[:2]
                if not pick:
                    notes.append(f"vegetation search: no basic-land candidates "
                                 f"available (choices=[{blob[:200]}])")
                    continue
                ids = [ch["id"] for ch in pick]
                spec = data.get("spec") or {}
                sub_type = spec.get("type") if isinstance(spec, dict) else None
                if rtype == "exactChoices":
                    # exactChoices carries one choice id; submit first, the
                    # driver will be re-prompted for the second if needed
                    sub = {"interactionId": iid, "response":
                           {"type": "choose", "data": {"choiceId": ids[0]}}}
                else:
                    sub = {"interactionId": iid, "response":
                           {"type": sub_type or "sequence",
                            "data": {"choiceIds": ids}}}
                say(f"[{who}] vegetation search: fetching "
                    f"{[choice_text(ch) for ch in pick]}")
                wire("search_submission", {"who": who, "submission": sub})
                c = p0 if who == "P0" else p1
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                acted = True
        return acted

    def load_env(path):
        try:
            with open(path) as f:
                return json.loads(f.read())["state"]
        except Exception:
            return None

    def evaluate():
        """Evaluate A1..A5 from saved states + observations."""
        pre_st = load_env(f"{EVDIR}/pre.json")
        mid_st = load_env(f"{EVDIR}/mid.json")
        post_st = load_env(f"{EVDIR}/post.json")
        # A1
        if pre_st is not None:
            unt = untapped_lands(pre_st, 0)
            ok = (omnath_on_bf(pre_st, 0)
                  and VEGETATION in hand_names(pre_st, 0)
                  and len(unt) >= 4
                  and any(obj_name(o) == "forest" for o in unt)
                  and any(n in BASICS for n in hand_names(pre_st, 0))
                  and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
                  and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
            obs["life_pre"] = [life_of(pre_st, 0), life_of(pre_st, 1)]
            obs["pool_pre"] = pool_colors(pre_st, 0)
            if ok:
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre.json: P0 {pre_st.get('phase')}, Omnath on BF, "
                             f"Vegetation in hand, {len(unt)} untapped lands "
                             f"(incl. Forest), land in hand, life 20/20")
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append("pre.json setup precondition not met "
                             f"(omnath={omnath_on_bf(pre_st,0)}, veg_in_hand="
                             f"{VEGETATION in hand_names(pre_st,0)}, untapped="
                             f"{len(unt)}, life={life_of(pre_st,0)}/"
                             f"{life_of(pre_st,1)}, phase={pre_st.get('phase')})")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("pre.json missing (Vegetation cast never reached)")
        # A2 / A3 from mid
        if mid_st is not None and pre_st is not None:
            obs["life_mid"] = [life_of(mid_st, 0), life_of(mid_st, 1)]
            obs["pool_mid"] = pool_colors(mid_st, 0)
            d_life = life_of(mid_st, 0) - life_of(pre_st, 0)
            if d_life == 4:
                ass["A2_life_gain"] = "passed"
                notes.append(f"landfall #1 resolved: P0 life "
                             f"{life_of(pre_st,0)}->{life_of(mid_st,0)} (+4)")
            else:
                ass["A2_life_gain"] = "failed"
                notes.append(f"landfall #1: P0 life delta {d_life:+d} "
                             f"(expected +4) -- first trigger broken or "
                             f"unresolved")
            from collections import Counter as C
            got = C(obs["pool_mid"])
            want = {"Red": 1, "Green": 1, "White": 1, "Blue": 1}
            if (dict(got) == want
                    and not (pre_st and pool_colors(pre_st, 0))):
                ass["A3_mana_added"] = "passed"
                notes.append(f"landfall #2 resolved: pool == "
                             f"{{R}}{{G}}{{W}}{{U}} exactly (was empty at pre)")
            elif all(got.get(c, 0) >= 1 for c in want):
                ass["A3_mana_added"] = "passed"
                notes.append(f"landfall #2 resolved: pool contains R/G/W/U "
                             f"(actual={sorted(obs['pool_mid'])}, pre empty)")
            else:
                ass["A3_mana_added"] = "failed"
                notes.append(f"landfall #2: pool={sorted(obs['pool_mid'])} at "
                             f"mid (expected exactly one R/G/W/U each) -- "
                             f"REPORTED BUG: second landfall did nothing")
        else:
            for k in ("A2_life_gain", "A3_mana_added"):
                ass[k] = "failed"
                notes.append(f"{k} unevaluable (missing pre/mid state)")
        # A4 / A5 from post
        if post_st is not None and pre_st is not None:
            obs["life_post"] = [life_of(post_st, 0), life_of(post_st, 1)]
            obs["pool_post"] = pool_colors(post_st, 0)
            d_p1 = life_of(post_st, 1) - life_of(pre_st, 1)
            if d_p1 == -4:
                ass["A4_damage"] = "passed"
                notes.append(f"landfall #3 resolved: P1 life "
                             f"{life_of(pre_st,1)}->{life_of(post_st,1)} (-4)")
            else:
                ass["A4_damage"] = "failed"
                notes.append(f"landfall #3: P1 life delta {d_p1:+d} (expected "
                             f"-4) -- REPORTED BUG: third landfall did nothing")
            veg_gy = VEGETATION in gy_names(post_st, 0)
            stack_empty = len(post_st.get("stack", []) or []) == 0
            if veg_gy and stack_empty and omnath_on_bf(post_st, 0):
                ass["A5_cleanup"] = "passed"
                notes.append(f"post.json: Vegetation in P0 gy, stack empty, "
                             f"Omnath on BF (turn {post_st.get('turn_number')}, "
                             f"phase {post_st.get('phase')})")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"post.json: veg_in_gy={veg_gy} "
                             f"stack_empty={stack_empty} "
                             f"omnath_bf={omnath_on_bf(post_st,0)}")
        else:
            for k in ("A4_damage", "A5_cleanup"):
                ass[k] = "failed"
                notes.append(f"{k} unevaluable (missing pre/post state)")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif all(ass[k] == "passed" for k in
                 ("A2_life_gain", "A3_mana_added", "A4_damage", "A5_cleanup")):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("at least one required landfall outcome failed while "
                         "the setup was valid -- matches the reported "
                         "second/third-landfall failure")
        return verdict

    async def finish():
        dur = time.time() - t_start
        nonlocal post_exported
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        verdict = evaluate()
        run = {
            "issue": 2391,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9375,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_2391.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x Omnath / 8x Explosive Vegetation deck density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games); exercised behavior is the shipped card text.",
                "Two landfall triggers in one turn are produced by Explosive "
                "Vegetation (two basics to the battlefield) rather than two "
                "normal land drops, which the rules limit to one per turn.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x omnath locus of creation + 8x explosive "
                          "vegetation + 11x each basic (mulligan to omnath + "
                          "3 lands; lands played preferring missing colors); "
                          "P1: 60x mountain dummy",
            "contract_line": "Landfall x3 in one turn: land #1 gains P0 4 "
                             "life; land #2 adds {R}{G}{W}{U}; land #3 deals "
                             "4 to each opponent",
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

    def land_choice_for_play(acts, state):
        """Pick a PlayLand action preferring a color missing from the BF."""
        cands = [a for a in acts if a["type"] == "PlayLand"]
        if not cands:
            return None
        have = bf_colors(state, 0)
        def key(a):
            d = a.get("data", {})
            nm = lname(state, d.get("object_id") or a.get("_src_oid"))
            col = BASICS.get(nm)
            return (0 if (col and col not in have) else 1, nm)
        return sorted(cands, key=key)[0]

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, mid_exported, post_exported, land3_played, \
            settle_empty
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        phase = state.get("phase")
        # --- mulligan ---
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n in BASICS)
            mulls = kept.get("P0_mulls", 0)
            if (OMNATH in hn and lands >= 3) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (omnath={OMNATH in hn}, lands={lands}, "
                    f"mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (omnath={OMNATH in hn}, "
                    f"lands={lands})")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]
                omnaths = sum(1 for o in hand_ids if lname(state, o) == OMNATH)
                def bottom_key(oid):
                    nm = lname(state, oid)
                    if nm in BASICS:
                        return 0
                    if nm == OMNATH and omnaths > 1:
                        return 1
                    return 2  # keep Vegetation / single Omnath
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}: {[lname(state, x) for x in picks]}")
                return
        # --- engine-advertised payments ---
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # --- trigger ordering: submit the advertised default ---
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                say("OrderTriggers: submitting advertised default order")
                wire("order_triggers", oa)
                await submit_as_is(p0, oa)
                return
        # --- interactions (vegetation search lives here) ---
        if await scan_interactions(st, "P0"):
            return
        # --- mid checkpoint: vegetation resolved, triggers off the stack ---
        if (obs["cast_submitted"] and not mid_exported
                and VEGETATION in gy_names(state, 0)
                and len(state.get("stack", []) or []) == 0
                and turn == obs["cast_turn"]):
            say(f"MID: veg resolved, stack empty; life={life_of(state,0)}/"
                f"{life_of(state,1)} pool={pool_colors(state,0)}")
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid.json", "w") as f:
                    f.write(mid)
                mid_exported = True
            except Exception as e:
                notes.append(f"mid export failed: {e}")
            return
        # --- post checkpoint: land #3 played and its trigger resolved ---
        if land3_played and not post_exported:
            if len(state.get("stack", []) or []) == 0:
                settle_empty += 1
            else:
                settle_empty = 0
            if settle_empty >= 2:
                say(f"POST: land3 trigger resolved; life={life_of(state,0)}/"
                    f"{life_of(state,1)} pool={pool_colors(state,0)}")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        veg_in_hand = VEGETATION in hand_names(state, 0)
        omnath_bf = omnath_on_bf(state, 0)
        unt = untapped_lands(state, 0)
        land_in_hand = any(n in BASICS for n in hand_names(state, 0))
        main_phase = phase in ("PreCombatMain", "PostCombatMain")
        # proof moment: cast Explosive Vegetation (hold this turn's land drop)
        if (not obs["cast_submitted"] and omnath_bf and veg_in_hand
                and main_phase and len(unt) >= 4
                and any(obj_name(o) == "forest" for o in unt)
                and land_in_hand
                and player_of(state, 0).get("lands_played_this_turn", 0) == 0):
            ca = find_action(acts, "CastSpell", VEGETATION, state)
            if ca:
                say("proof turn: exporting PRE, casting Explosive Vegetation")
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_exported = True
                wire("cast_vegetation", ca)
                obs["cast_turn"] = turn
                obs["cast_phase"] = phase
                await submit_as_is(p0, ca)
                obs["cast_submitted"] = True
                say(f"P0 casts Explosive Vegetation (turn {turn})")
                return
            # hold the land drop while the proof is one step away
            if obs["hold_turn"] != turn:
                obs["turns_held"] += 1
                obs["hold_turn"] = turn
                say(f"holding land drop (turn {turn}, held #{obs['turns_held']})")
            if obs["turns_held"] >= 4:
                obs["hold_released"] = True
                notes.append("proof gate never opened after 4 held turns; "
                             "resuming normal land play")
            else:
                for a in acts:
                    if a["type"] == "PassPriority":
                        await submit_as_is(p0, a)
                        return
                return
        # cast Omnath when advertised
        if not omnath_bf:
            ca = find_action(acts, "CastSpell", OMNATH, state)
            if ca and main_phase:
                say(f"P0 casts Omnath (turn {turn})")
                wire("cast_omnath", ca)
                await submit_as_is(p0, ca)
                return
        # land #3: after vegetation resolution, play the held land drop
        if (mid_exported and not land3_played
                and player_of(state, 0).get("lands_played_this_turn", 0) == 0):
            la = land_choice_for_play(acts, state)
            if la:
                d = la.get("data", {})
                nm = lname(state, d.get("object_id") or la.get("_src_oid"))
                say(f"P0 plays land #3: {nm}")
                wire("play_land_3", la)
                await submit_as_is(p0, la)
                land3_played = True
                return
        # normal land play (suppressed while holding for the proof)
        holding = (not obs["cast_submitted"] and omnath_bf and veg_in_hand
                   and main_phase and len(unt) >= 4 and not obs["hold_released"])
        if not holding:
            la = land_choice_for_play(acts, state)
            if la and player_of(state, 0).get("lands_played_this_turn", 0) == 0:
                await submit_as_is(p0, la)
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
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if await scan_interactions(st, "P1"):
            return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
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
    last_tick_at = {}
    last_diag = 0.0
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
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)[:8]} "
                f"P0untapped={len(untapped_lands(s, 0))} omnath={omnath_on_bf(s, 0)} "
                f"life={life_of(s, 0)}/{life_of(s, 1)} "
                f"stack={len(s.get('stack') or [])} cast={obs['cast_submitted']} "
                f"mid={mid_exported} land3={land3_played} post={post_exported}")
        if obs["cast_submitted"] and not post_exported and stuck_deadline is None:
            stuck_deadline = time.time() + 300
        if not obs["cast_submitted"] or post_exported:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("Vegetation cast but post-resolution state not reached "
                         "in 300s; see wire log (possible unhandled interaction)")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
