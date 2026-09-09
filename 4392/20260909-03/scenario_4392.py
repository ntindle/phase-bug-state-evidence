#!/usr/bin/env python3
"""Issue #4392: "Stuck decision: ChooseManaColor" (treasure color choice softlock).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-06-26, Build v0.6.0/2855322, browser): game stuck with
diagnostic "Waiting for: ChooseManaColor / Stuck players: 0".
Reporter follow-up (nickjcherms-debug, 2026-07-19): "Treasure doubling
effects likes alchemist talent and [Goldspan Dragon]".

Triage comment (mike-theDude, 2026-07-26): "PR #5141 (c8198e7f5) fixed this
exact ChooseManaColor softlock. The Treasure sacrificed itself before color
selection, after which the engine tried to read the mana ability from the
vanished source. Mana abilities are now snapshotted at activation, and the
current Goldspan-style Treasure regression proves that choosing a color
still succeeds after the Treasure is gone and produces two mana."

Oracle text (verified from pinned v0.78.0 card-data.json):
  goldspan dragon: "Treasures you control have '{T}, Sacrifice this artifact:
      Add two mana of any one color.'"
  strike it rich: "Create a Treasure token. (It's an artifact with '{T},
      Sacrifice this token: Add one mana of any color.')"

Scenario (native engine, two human-client seats):
  P0: 20x strike it rich ({R}) + 12x goldspan dragon ({3}{R}{R}) + 28x mountain
      (dense spell counts: engine accepts >4-of for custom games; mulligan
      hunts Strike It Rich + 2 lands).
  P1: 60x mountain dummy (plays land, passes; never attacks).

  TEST 1 (plain treasure, control): activate an untapped Treasure's mana
      ability "{T}, Sacrifice this: Add one mana of any color". If a color
      choice is offered, answer Red (deterministic policy). Expected: the
      choice is accepted, the game is NOT stuck, P0's mana pool gains 1 Red.

  TEST 2 (reported path): cast Goldspan Dragon; activate an untapped
      Treasure whose ability is now "{T}, Sacrifice this artifact: Add two
      mana of any one color". Answer Red if asked. Expected: the choice is
      accepted even though the Treasure sacrificed itself, P0's pool gains
      2 Red, game proceeds.

Assertions:
  A1_setup_ok      pre_plain.json: P0 main phase, >=1 untapped Treasure
                   controlled by P0, life 20/20.
  A2_plain_treasure post_plain.json: P0 pool red-delta == +1 vs pre_plain;
                   that Treasure left the battlefield (sacrificed); no stuck
                   color decision remained.
  A3_goldspan_setup pre_gold.json: Goldspan Dragon on battlefield under P0,
                   >=1 untapped Treasure controlled by P0.
  A4_goldspan_treasure post_gold.json: P0 pool red-delta == +2 vs pre_gold;
                   the Treasure left the battlefield; no stuck color decision.
  A5_no_stuck      after both activations: waiting_for is not a color-choice
                   / stuck decision; game continues at priority.

Verdict rule: not-reproduced iff A2, A4 and A5 pass (color choices were
answerable and the mana was produced on v0.78.0). reproduced iff a
ChooseManaColor decision becomes unanswerable (no available choices, choice
submission rejected, or the decision pending with no responder) or the mana
fails to appear after an accepted choice. blocked iff the game cannot be
driven to a treasure activation.

Evidence: evidence/4392/<run-id>/pre_plain.json, post_plain.json,
pre_gold.json, post_gold.json, run.json, manifest.sha256, summary.png,
scenario_4392.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-03"
EVDIR = f"{BACKFILL}/evidence/4392/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

STRIKE = "strike it rich"
GOLDSPAN = "goldspan dragon"
MOUNTAIN = "mountain"

P0_DECK = [(STRIKE, 20), (GOLDSPAN, 12), (MOUNTAIN, 28)]
P1_DECK = [(MOUNTAIN, 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-03",
}

COLORS = ("white", "blue", "black", "red", "green", "colorless")
SYMBOL_TO_COLOR = {"W": "white", "U": "blue", "B": "black",
                   "R": "red", "G": "green", "C": "colorless"}


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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


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


def untapped_mountains(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == MOUNTAIN and not o.get("tapped")]


def treasures(state, pid, untapped_only=True):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                and "treasure" in obj_name(o):
            if untapped_only and o.get("tapped"):
                continue
            out.append(int(oid))
    return out


def goldspan_on_bf(state, pid):
    return any(o.get("zone") == "Battlefield" and o.get("controller") == pid
               and obj_name(o) == GOLDSPAN
               for o in state.get("objects", {}).values())


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def mana_pool(state, pid):
    out = Counter()
    for p in state.get("players", []):
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in COLORS:
                    if color in blob:
                        out[color] += 1
                        break
                else:
                    out["unknown"] += 1
    return dict(out)


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    if not t:
        # mana-choice surfaces carry symbols, not text
        # (e.g. ChooseManaColor: {"role": "manaChoice", "symbols": ["R"]})
        syms = mana_symbols(ch)
        if syms:
            t = "/".join(SYMBOL_TO_COLOR.get(x, x) for x in syms)
    return str(t)


def mana_symbols(ch):
    """Mana symbols advertised on a choice's surfaces (may be empty)."""
    out = []
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict):
            for sym in d.get("symbols") or []:
                if sym not in out:
                    out.append(sym)
    return out


def is_color_prompt(opp):
    """True if the opportunity looks like a mana-color choice."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else ""
    choices = data.get("choices") or data.get("candidates") or []
    texts = [choice_text(ch).lower() for ch in choices]
    has_colors = any(any(c in t for c in ("white", "blue", "black", "red", "green"))
                     for t in texts)
    has_mana_role = any(
        any((s.get("data") or {}).get("role") == "manaChoice"
            for s in ch.get("surfaces", []) or [])
        for ch in choices)
    blob = json.dumps(opp, default=str).lower().replace(" ", "").replace("_", "")
    return (stype == "manaGroups" or has_mana_role or has_colors
            or "manacolor" in blob or "choosemana" in blob), texts


async def main():
    t_start = time.time()
    notes = []
    notes.append("driver fix vs first attempt of this run: an early "
                 "'active_player != 0 -> return' guard in p0_tick deadlocked "
                 "P1's upkeep (P0 holds priority there after P1 passes and "
                 "must pass it back). Rewrote p0_tick 1488-style: P0 passes "
                 "priority on any turn, test activations only on own main "
                 "phase. First attempt produced no engine behavior; restarted "
                 "fresh in the same run dir.")
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_plain_treasure", "A3_goldspan_setup",
            "A4_goldspan_treasure", "A5_no_stuck")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    obs = {
        "color_choice_seen": False,
        "color_choice_answered": 0,
        "color_choice_submissions": [],
        "color_choice_opportunities": [],
        "rejections": [],
        "test1": {"activated_oid": None, "choice_answered": False,
                  "pool_before": None, "pool_after": None,
                  "treasure_count_before": None, "treasure_count_after": None,
                  "done": False},
        "test2": {"activated_oid": None, "choice_answered": False,
                  "pool_before": None, "pool_after": None,
                  "treasure_count_before": None, "treasure_count_after": None,
                  "done": False},
        "goldspan_cast": False,
        "stuck_evidence": [],
    }
    finished_tests = {"t1": False, "t2": False}

    # drain inbox noise helper: record rejections
    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def color_choice_in(vi):
        """Return (opp, choice_id_for_red, rtype) for the first answerable
        color-choice opportunity, else None. Records opportunity shapes."""
        if not vi:
            return None
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            looks, texts = is_color_prompt(opp)
            if not looks:
                continue
            choices = data.get("choices") or data.get("candidates") or []
            avail = [(ch, choice_text(ch)) for ch in choices
                     if ch.get("status", {}).get("type") == "available"]
            rec = {"interactionId": iid, "rtype": rtype,
                   "choice_texts": texts[:10],
                   "available_count": len(avail),
                   "raw": opp}
            if rec not in obs["color_choice_opportunities"][-3:]:
                obs["color_choice_opportunities"].append(
                    {k: v for k, v in rec.items() if k != "raw"})
                wire("color_choice_opportunity",
                     {k: v for k, v in rec.items()})
                say(f"COLOR-CHOICE opportunity rtype={rtype} avail={len(avail)} "
                    f"texts={texts[:8]}")
            if not obs["color_choice_seen"]:
                obs["color_choice_seen"] = True
            pick = None
            for ch, txt in avail:
                if "red" in txt.lower() or "R" in mana_symbols(ch):
                    pick = ch
                    break
            if pick is None:
                if not any(r.get("no_red_choice") for r in obs["stuck_evidence"]):
                    obs["stuck_evidence"].append(
                        {"kind": "no_red_choice",
                         "interactionId": iid, "texts": texts[:10]})
                continue
            return opp, pick.get("id"), rtype, resp
        return None

    async def answer_red(c, found):
        opp, cid, rtype, resp = found
        iid = opp.get("interactionId")
        spec = (resp.get("data", {}) or {}).get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        else:
            # schema path: submit the spec variant. manaGroups
            # (InteractionResponse::ManaGroups { choice_ids, count })
            # requires 1 <= count <= max_batch (cf. #1234 driver notes).
            rdata = {"choiceIds": [cid]}
            if stype == "manaGroups":
                max_batch = ((spec.get("data") or {}).get("maxBatch")
                             if isinstance(spec.get("data"), dict) else None) or 1
                rdata["count"] = min(1, max_batch)
            sub = {"interactionId": iid,
                   "response": {"type": stype or "sequence", "data": rdata}}
        say(f"[{c.name}] answers color choice with RED (choice {cid}, rtype={rtype})")
        wire("color_choice_submission", {"who": c.name, "submission": sub})
        obs["color_choice_submissions"].append(
            {"who": c.name, "interactionId": iid, "choice": "red"})
        obs["color_choice_answered"] += 1
        await c.send_interaction(sub)

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    async def activate_treasure(state, acts, tag, file_tag, want_mana):
        """Export pre_<file_tag>, submit ActivateAbility for one untapped
        treasure, selecting the ability whose AnyOneColor production count
        matches want_mana (1 = base treasure ability, 2 = Goldspan-granted).
        The ability list is engine-issued state data; matching the intended
        decision against it is required — index 0 is not always the right one.
        """
        tids = treasures(state, 0)
        if not tids:
            notes.append(f"{tag}: no untapped treasure available")
            return False
        tid = tids[0]
        want_idx = None
        for i, a in enumerate(get_obj(state, tid).get("abilities", []) or []):
            try:
                produced = (a.get("effect") or {}).get("produced") or {}
                cnt = produced.get("count") or {}
                val = cnt.get("value") if isinstance(cnt, dict) else None
                if produced.get("type") == "AnyOneColor" and val == want_mana:
                    want_idx = i
                    break
            except Exception:
                continue
        cands = [a for a in acts
                 if a["type"] == "ActivateAbility"
                 and a.get("data", {}).get("source_id") == tid]
        wire("treasure_ability_candidates",
             {"tag": tag, "treasure_oid": tid, "want_mana": want_mana,
              "want_index": want_idx,
              "candidates": [{"ability_index": a["data"].get("ability_index")}
                             for a in cands]})
        say(f"{tag}: treasure {tid} ability candidates "
            f"{[a['data'].get('ability_index') for a in cands]}, want_mana={want_mana} -> index {want_idx}")
        aa = next((a for a in cands
                   if a.get("data", {}).get("ability_index") == want_idx), None)
        if aa is None:
            notes.append(f"{tag}: no ActivateAbility with ability_index={want_idx} "
                         f"(want_mana={want_mana}) for treasure {tid}")
            return False
        cfg = obs[tag]
        cfg["activated_oid"] = tid
        cfg["ability_index"] = want_idx
        cfg["pool_before"] = mana_pool(state, 0)
        cfg["treasure_count_before"] = len(treasures(state, 0, untapped_only=False))
        await export(f"pre_{file_tag}")
        say(f"{tag}: activating treasure {tid} ability_index={want_idx} via ActivateAbility")
        wire("treasure_activation", {"tag": tag, "treasure_oid": tid, "action": aa})
        await submit_as_is(p0, aa)
        return True

    def p0_priority(state):
        return ((state.get("waiting_for") or {}).get("type") == "Priority"
                and state.get("priority_player") == 0)

    def log_vi(c, st, tag):
        """Log every viewer_interaction opportunity verbosely (diagnostic for
        ChooseManaColor-style stuck decisions)."""
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return
        for opp in opps:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            choices = data.get("choices") or data.get("candidates") or []
            info = {
                "tag": tag,
                "who": c.name,
                "canSubmit": vi.get("canSubmit"),
                "interactionId": opp.get("interactionId"),
                "rtype": resp.get("type"),
                "spec": (data.get("spec") or {}).get("type")
                        if isinstance(data.get("spec"), dict) else None,
                "spec_raw": json.dumps(data.get("spec"), default=str)[:500],
                "n_choices": len(choices),
                "choices": [
                    {"id": ch.get("id"), "text": choice_text(ch)[:60],
                     "status": (ch.get("status") or {}).get("type"),
                     "raw": json.dumps(ch, default=str)[:400]}
                    for ch in choices[:12]],
            }
            wire("vi_opportunity", info)
            say(f"[vi {tag}/{c.name}] canSubmit={info['canSubmit']} "
                f"rtype={info['rtype']} n={info['n_choices']} "
                f"choices={[(ch['text'], ch['status']) for ch in info['choices']][:6]}")

    async def p0_tick(st, acts, state):
        # NOTE: P0 must pass priority on opponent turns too (like the 1488
        # driver): priority_player==0 happens on P1's turn after P1 passes.
        wtype = (state.get("waiting_for") or {}).get("type")

        # mulligan: hunt for Strike It Rich + lands (deterministic-ish setup)
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n == MOUNTAIN)
            mulls = kept.get("P0_mulls", 0)
            # 20x Strike It Rich in 60 -> ~95% in the opener; mull hard for it
            if (STRIKE in hn and lands >= 2) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (strike={STRIKE in hn}, lands={lands}, mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (strike={STRIKE in hn}, lands={lands})")
            return True
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
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]
                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm == MOUNTAIN else (2 if nm in (STRIKE, GOLDSPAN) else 1)
                picks = sorted(hand_ids, key=bkey)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
            return True

        # payments first
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return True

        # answer any color-choice interaction before other actions
        vi = get_vi(st)
        if vi:
            found = color_choice_in(vi)
            if found:
                rev_now = state.get("state_revision", 0)
                if not finished_tests["t1"]:
                    obs["test1"]["choice_answered"] = True
                    obs["test1"]["answered_rev"] = rev_now
                elif not finished_tests["t2"]:
                    obs["test2"]["choice_answered"] = True
                    obs["test2"]["answered_rev"] = rev_now
                await answer_red(p0, found)
                return True

        # combat declarations: never attack/block (keep the board simple)
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True

        # ---- test 1 completion check ----
        if obs["test1"]["activated_oid"] and not finished_tests["t1"]:
            pool = mana_pool(state, 0)
            pb = obs["test1"]["pool_before"] or {}
            red_delta = pool.get("red", 0) - pb.get("red", 0)
            tcount = len(treasures(state, 0, untapped_only=False))
            answered = obs["test1"]["choice_answered"]
            rev = state.get("state_revision", 0)
            arev = obs["test1"].get("answered_rev", 0)
            wf = (state.get("waiting_for") or {}).get("type")
            resolved = (red_delta > 0) or (answered and rev > arev
                                            and wf == "Priority")
            if resolved:
                obs["test1"]["pool_after"] = pool
                obs["test1"]["treasure_count_after"] = tcount
                finished_tests["t1"] = True
                await export("post_plain")
                say(f"TEST1 done: red_delta={red_delta} treasure {obs['test1']['treasure_count_before']}->{tcount}")
                return True
        # ---- test 2 completion check ----
        if obs["test2"]["activated_oid"] and not finished_tests["t2"]:
            pool = mana_pool(state, 0)
            pb = obs["test2"]["pool_before"] or {}
            red_delta = pool.get("red", 0) - pb.get("red", 0)
            tcount = len(treasures(state, 0, untapped_only=False))
            answered = obs["test2"]["choice_answered"]
            rev = state.get("state_revision", 0)
            arev = obs["test2"].get("answered_rev", 0)
            wf = (state.get("waiting_for") or {}).get("type")
            resolved = (red_delta > 0) or (answered and rev > arev
                                            and wf == "Priority")
            if resolved:
                obs["test2"]["pool_after"] = pool
                obs["test2"]["treasure_count_after"] = tcount
                finished_tests["t2"] = True
                await export("post_gold")
                say(f"TEST2 done: red_delta={red_delta} treasure {obs['test2']['treasure_count_before']}->{tcount}")
                return True

        if not p0_priority(state):
            return False

        # protect mid-flight tests: never pass priority while an activation's
        # result is pending, or a phase advance would empty the pool mid-proof
        mid_flight = ((obs["test1"]["activated_oid"] and not finished_tests["t1"])
                      or (obs["test2"]["activated_oid"] and not finished_tests["t2"]))
        if mid_flight:
            return False

        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main and not obs["test2"]["activated_oid"]:
            # ---- TEST 1 trigger (plain treasure: base 1-mana ability) ----
            if not obs["test1"]["activated_oid"] and treasures(state, 0):
                await activate_treasure(state, acts, "test1", "plain", 1)
                return True
            # ---- development: play land, cast spells ----
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return True
            # keep a treasure supply: cast Strike It Rich whenever able
            sid = spell_in_hand_oid(state, 0, STRIKE)
            if sid is not None and len(untapped_mountains(state, 0)) >= 1:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == STRIKE:
                        say("P0 casts Strike It Rich")
                        wire("cast_strike", a)
                        await submit_as_is(p0, a)
                        return True
            # cast Goldspan Dragon once (needs 3RR -> 5 untapped mountains)
            gid = spell_in_hand_oid(state, 0, GOLDSPAN)
            if (gid is not None and not goldspan_on_bf(state, 0)
                    and len(untapped_mountains(state, 0)) >= 5):
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == GOLDSPAN:
                        say("P0 casts Goldspan Dragon")
                        wire("cast_goldspan", a)
                        await submit_as_is(p0, a)
                        obs["goldspan_cast"] = True
                        return True
            # ---- TEST 2 trigger (Goldspan-style: granted 2-mana ability) ----
            if (goldspan_on_bf(state, 0) and treasures(state, 0)
                    and finished_tests["t1"]):
                await activate_treasure(state, acts, "test2", "gold", 2)
                return True

        # default: pass priority (also on opponent's turns)
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps")
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return True
        vi = get_vi(st)
        if vi:
            found = color_choice_in(vi)
            if found:
                # P1 should never get the treasure color choice, but answer
                # rather than stall if it does
                notes.append("unexpected: color choice offered to P1")
                await answer_red(p1, found)
                return True
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if wtype != "Priority" or state.get("priority_player") != 1:
            return False
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        pre1 = load_env("pre_plain"); post1 = load_env("post_plain")
        pre2 = load_env("pre_gold"); post2 = load_env("post_gold")
        stuck_types = [w for w in obs["stuck_evidence"]]
        notes.append(f"color_choice_seen={obs['color_choice_seen']} "
                     f"answered={obs['color_choice_answered']} "
                     f"rejections={len(obs['rejections'])}")
        # A1
        if pre1 is not None:
            s = pre1["state"]
            ok = (len(treasures(s, 0)) >= 1
                  and s.get("phase") in ("PreCombatMain", "PostCombatMain")
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: pre_plain has {len(treasures(s,0))} untapped treasure(s), "
                         f"phase={s.get('phase')}, life={life_of(s,0)}/{life_of(s,1)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre_plain.json missing (never reached test 1)")
        # A2
        if pre1 is not None and post1 is not None:
            s0, s1 = pre1["state"], post1["state"]
            d_red = (mana_pool(s1, 0).get("red", 0)
                     - mana_pool(s0, 0).get("red", 0))
            t0 = len([o for oid, o in s0.get("objects", {}).items()
                      if "treasure" in obj_name(o)
                      and o.get("zone") == "Battlefield"
                      and o.get("controller") == 0])
            t1 = len([o for oid, o in s1.get("objects", {}).items()
                      if "treasure" in obj_name(o)
                      and o.get("zone") == "Battlefield"
                      and o.get("controller") == 0])
            wf = (s1.get("waiting_for") or {}).get("type")
            stuck = "color" in str(wf).lower()
            if d_red == 1 and t1 == t0 - 1 and not stuck:
                ass["A2_plain_treasure"] = "passed"
            else:
                ass["A2_plain_treasure"] = "failed"
            notes.append(f"A2: plain treasure red-delta={d_red} (want 1), "
                         f"treasures {t0}->{t1}, choice_answered="
                         f"{obs['test1']['choice_answered']}, post waiting_for={wf}")
        else:
            ass["A2_plain_treasure"] = "failed"
            notes.append("A2: missing pre/post plain states")
        # A3
        if pre2 is not None:
            s = pre2["state"]
            ok = goldspan_on_bf(s, 0) and len(treasures(s, 0)) >= 1
            ass["A3_goldspan_setup"] = "passed" if ok else "failed"
            notes.append(f"A3: pre_gold goldspan_on_bf={goldspan_on_bf(s,0)}, "
                         f"untapped treasures={len(treasures(s,0))}")
        else:
            ass["A3_goldspan_setup"] = "failed"
            notes.append("A3: pre_gold.json missing (goldspan path not reached)")
        # A4
        if pre2 is not None and post2 is not None:
            s0, s1 = pre2["state"], post2["state"]
            d_red = (mana_pool(s1, 0).get("red", 0)
                     - mana_pool(s0, 0).get("red", 0))
            t0 = len([o for oid, o in s0.get("objects", {}).items()
                      if "treasure" in obj_name(o)
                      and o.get("zone") == "Battlefield"
                      and o.get("controller") == 0])
            t1 = len([o for oid, o in s1.get("objects", {}).items()
                      if "treasure" in obj_name(o)
                      and o.get("zone") == "Battlefield"
                      and o.get("controller") == 0])
            wf = (s1.get("waiting_for") or {}).get("type")
            stuck = "color" in str(wf).lower()
            if d_red == 2 and t1 == t0 - 1 and not stuck:
                ass["A4_goldspan_treasure"] = "passed"
            else:
                ass["A4_goldspan_treasure"] = "failed"
            notes.append(f"A4: goldspan treasure red-delta={d_red} (want 2), "
                         f"treasures {t0}->{t1}, choice_answered="
                         f"{obs['test2']['choice_answered']}, post waiting_for={wf}")
        else:
            ass["A4_goldspan_treasure"] = "failed"
            notes.append("A4: missing pre/post gold states")
        # A5: no stuck decision anywhere after the activations
        final = post2 if post2 is not None else post1
        if final is not None:
            wf = (final["state"].get("waiting_for") or {}).get("type")
            stuck = ("color" in str(wf).lower()
                     or len(stuck_types) > 0
                     or any(r["type"] == "ActionRejected"
                            for r in obs["rejections"]))
            ass["A5_no_stuck"] = "failed" if stuck else "passed"
            notes.append(f"A5: final waiting_for={wf}, stuck_evidence={stuck_types}, "
                         f"rejections={len(obs['rejections'])}")
        else:
            ass["A5_no_stuck"] = "failed"
            notes.append("A5: no final post state")
        # verdict
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached a treasure activation")
        elif (ass["A2_plain_treasure"] == "passed"
              and ass["A4_goldspan_treasure"] == "passed"
              and ass["A5_no_stuck"] == "passed"):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("the treasure color-choice path did not behave per "
                         "Oracle on this build; see assertion notes")
        return verdict

    async def finish():
        dur = time.time() - t_start
        for tag in ("post_plain", "post_gold"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": 4392,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4392.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k != "color_choice_opportunities"}
            | {"color_choice_opportunity_count":
               len(obs["color_choice_opportunities"])},
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                            "12x spell density is a test-harness convenience "
                            "(engine accepts >4-of for custom games).",
                            "Color choice answered with 'Red' as the deterministic test "
                            "policy; only the reported two-mana (Goldspan-style) and the "
                            "one-mana (plain) treasure paths were exercised."],
            "setup_line": "P0: 12x strike it rich + 12x goldspan dragon + 36x mountain; "
                          "P1: 60x mountain dummy",
            "contract_line": "Activate plain Treasure ('{T}, sac: add one mana of any "
                             "color') and Goldspan-style Treasure ('{T}, sac: add two "
                             "mana of any one color'): answer the color choice, confirm "
                             "the mana arrives and the game is not stuck at "
                             "ChooseManaColor",
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

    # copy scenario into evidence dir for provenance
    with open(f"{EVDIR}/scenario_4392.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_4392.py").read())

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            await drain_rejections(c)
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
            # verbose interaction logging while a test activation is mid-flight,
            # or whenever the game waits on a non-priority decision
            mid = ((obs["test1"]["activated_oid"] and not finished_tests["t1"])
                   or (obs["test2"]["activated_oid"] and not finished_tests["t2"]))
            wf_now = ((st.get("state", {}) or {}).get("waiting_for") or {}).get("type")
            if mid or (wf_now and wf_now != "Priority"):
                log_vi(c, st, "midflight" if mid else f"wf-{wf_now}")
        if finished_tests["t2"]:
            say("both tests complete; finishing")
            await finish()
            return
        if p0.latest and p0.latest["state"].get("turn_number", 0) >= 18 \
                and not (finished_tests["t1"] and finished_tests["t2"]):
            notes.append("turn 18 reached without completing both tests; "
                         "bailing out to evaluation")
            say("turn 18 bail-out; finishing")
            await finish()
            return
        # stuck watch: an activated treasure whose choice never resolves
        mid = ((obs["test1"]["activated_oid"] and not finished_tests["t1"])
               or (obs["test2"]["activated_oid"] and not finished_tests["t2"]))
        if mid and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not mid:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            obs["stuck_evidence"].append(
                {"kind": "activation_never_completed",
                 "waiting_for": wf,
                 "priority_player": s.get("priority_player")})
            say(f"STUCK WATCH FIRED: waiting_for={wf}")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} hand={hand_names(s,0)[:6]} "
                f"treasures={len(treasures(s,0,untapped_only=False))} "
                f"goldspan={goldspan_on_bf(s,0)} pool={mana_pool(s,0)} "
                f"t1={finished_tests['t1']} t2={finished_tests['t2']} "
                f"color_seen={obs['color_choice_seen']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
