#!/usr/bin/env python3
"""Issue #1235: feasible_mana_capacity sum over-counts chain-sacrifice configurations.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Report (github #1235, status:confirmed, area:engine, mechanic:mana,
priority:p2-wrong-game-result): `feasible_mana_capacity` returns the
single-shot maximum yield per permanent; the `can_feasibly_pay_mana_cost`
(casting.rs) and `max_x_value_excluding` (casting_costs.rs) call sites sum that
value across the whole battlefield, over-counting chain-sacrifice
configurations where one permanent's activation consumes another's existence.

Oracle text (pinned card-data.json):
  Krark-Clan Ironworks: "Sacrifice an artifact: Add {C}{C}."
  Phyrexian Altar: "Sacrifice a creature: Add one mana of any color."
  Fireball: "{X}{R}" -- deals X damage divided among any number of targets.
  Banefire: "{X}{R}" -- deals X damage to any target (single target; used
  instead of Fireball to avoid the DistributeAmong damage-division prompt).
  Endless One: "{X}" -- enters with X +1/+1 counters.

Acceptance criteria from the issue:
  (1) max_x_value for a {X}{R} cost with 2 KCI + 1 Mountain + 1 fodder
      reports X = 2 (not X = 4).
  (2) max_x_value for a {X} cost with 1 KCI + 1 Phyrexian Altar + 1 creature
      reports X = 2 (not X = 3 or X = 4).
Downstream impact claimed: the X chooser shows the over-counted cap and
manual-mode payment then fails after the player has committed.

Three games (native engine, two human-client seats):
  Game A: 2 Krark-Clan Ironworks + Mountains + 1 Memnite (artifact fodder),
          Banefire ({X}{R}) in hand, empty pool, own main-phase priority.
          Submit CastSpell -> read the X-choice number schema's max and
          compare against the bounded-flow acceptance value
          (n_mountains + 2*min(n_kci, n_fodder) - 1 for the {R} shard).
          Then choose X=0, target P1, and complete the cast (control).
  Game B: 1 KCI + 1 Phyrexian Altar + 1 Memnite (shared fuel),
          Endless One ({X}) in hand. Submit CastSpell -> read X max.
          Then commit X=3 (the over-counted cap) and attempt a good-faith
          manual payment: KCI activation(s) + auto PayMana; never cancel.
          Record whether the cast completes or stalls unpayable.
  Game C: same board as B. Commit X=2 (the acceptance cap): KCI activation
          (sac Memnite -> {C}{C}) must pay {2}; Endless One must enter with
          exactly 2 +1/+1 counters (control: the acceptance cap is reachable).

Assertions:
  A1_setup_ok          game A board/hand/phase/pool preconditions reached
  A2_x_cap_overcounted game A X-choice max exceeds the bounded-flow
                       acceptance value (bug signature: each KCI counts the
                       same single Memnite)
  A3_cast_x0_completes game A Banefire X=0 resolves: Banefire in graveyard,
                       P1 life still 20
  B1_setup_ok          game B preconditions reached
  B2_x_cap_overcounted game B X-choice max exceeds the bounded-flow
                       acceptance value n_mountains + 2 (bug signature:
                       KCI and Altar both count the same single Memnite)
  B3_x3_payment        documentary: outcome of the good-faith manual payment
                       for committed X=3 ("completed_via_self_cannibalization"
                       if KCI sacrifices itself for the last {C}{C},
                       "completed", or "stalled_unpayable" for a genuine
                       stall: pending cast, insufficient pool, no mana
                       activation offered, server healthy)
  B3_x3_counters       documentary: Endless One counters after resolution
  C1_setup_ok          game C preconditions reached
  C2_x_cap_observed    game C X-choice schema max (recorded)
  C3_cast_x2_completes game C Endless One X=2 enters with exactly 2 counters

Verdict rule: reproduced iff A2 or B2 shows the X-choice max above the
bounded-flow acceptance value for the observed board (the per-permanent sum
over-counts the shared fuel). not-reproduced iff both maxima equal their
acceptance values. blocked iff a setup never completes.
B3 is documentary either way (it tests the issue's downstream claim against
the real engine, including whether self-cannibalization is permitted).

Evidence: evidence/1235/<run-id>/{pre,post}_{A,B,C}.json, xchoice_{A,B,C}.json,
run.json, manifest.sha256, summary.png, scenario_1235.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-31"
EVDIR = f"{BACKFILL}/evidence/1235/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

P1_DECK = [("Forest", 60)]

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
    "source": "ServerHello (0.77.0/61715b5/protocol 67) + sha256 of the "
              "listening phase-server started by today's earlier backfill run "
              "(same pinned release); reused per task body since "
              "127.0.0.1:9374 was already listening",
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


# ---------------------------------------------------------------- helpers

def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def graveyard_names(state, pid):
    return [(o.get("base_name") or o.get("name") or "?")
            for o in state.get("objects", {}).values()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name
            and not o.get("tapped")]


def mana_pool(state, pid):
    out = Counter()
    for p in state.get("players", []):
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in ("white", "blue", "black", "red", "green",
                              "colorless"):
                    if color in blob:
                        out[color] += 1
                        break
                else:
                    out["unknown"] += 1
    return dict(out)


def pool_total(state, pid):
    return sum(mana_pool(state, pid).values())


def plus_counters(state, oid):
    o = state.get("objects", {}).get(str(oid), {})
    v = o.get("counters")
    if isinstance(v, dict):
        return int(v.get("P1P1", 0))
    if isinstance(v, int):
        return v
    return 0


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def choice_text(ch):
    bits = []

    def rec(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if k in ("name", "code", "label", "value", "description",
                         "text", "prompt", "title") and isinstance(val, str):
                    bits.append(val)
                else:
                    rec(val)
        elif isinstance(v, list):
            for x in v:
                rec(x)
    rec(ch.get("surfaces", []))
    return " | ".join(bits)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cast_spell_offered(state, st, spell):
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and obj_name(state, a.get("data", {}).get("object_id")) == spell:
            return True, a
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "castSpell" in codes and spell.lower() in choice_text(ch).lower():
                    return True, {"_vi_choice": ch, "_vi_opp": opp}
    return False, None


def find_number_opp(vi):
    """X-choice opportunity: schema with spec.type == 'number'. Returns
    (opp, min, max) or None."""
    if not vi:
        return None
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if spec.get("type") == "number":
            return opp, spec.get("min"), spec.get("max")
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def submit_vi(c, opp, choice, seq=False):
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
            rdata["count"] = 1
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": stype or "sequence", "data": rdata}}
    await c.send_interaction(sub)
    return sub


async def submit_number(c, opp, value):
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "number", "data": {"value": int(value)}}}
    await c.send_interaction(sub)
    return sub

# ---------------------------------------------------------------- game configs

GAMES = [
    {
        "tag": "A",
        "spell": "Banefire",
        "cost": "{X}{R}",
        "deck": [("Banefire", 12), ("Krark-Clan Ironworks", 12),
                 ("Memnite", 12), ("Mountain", 24)],
        "land": "Mountain",
        "need": {"Krark-Clan Ironworks": 2, "Memnite": 1, "Mountain": 1},
        "cast_cost": {"Krark-Clan Ironworks": 4},
        "desired_x": 0,
        "mode": "x0_control",
    },
    {
        "tag": "B",
        "spell": "Endless One",
        "cost": "{X}",
        "deck": [("Endless One", 12), ("Krark-Clan Ironworks", 8),
                 ("Phyrexian Altar", 4), ("Memnite", 12), ("Mountain", 24)],
        "land": "Mountain",
        "need": {"Krark-Clan Ironworks": 1, "Phyrexian Altar": 1,
                 "Memnite": 1},
        "cast_cost": {"Krark-Clan Ironworks": 4, "Phyrexian Altar": 3},
        "desired_x": 3,
        "mode": "overcommit",
    },
    {
        "tag": "C",
        "spell": "Endless One",
        "cost": "{X}",
        "deck": [("Endless One", 12), ("Krark-Clan Ironworks", 8),
                 ("Phyrexian Altar", 4), ("Memnite", 12), ("Mountain", 24)],
        "land": "Mountain",
        "need": {"Krark-Clan Ironworks": 1, "Phyrexian Altar": 1,
                 "Memnite": 1},
        "cast_cost": {"Krark-Clan Ironworks": 4, "Phyrexian Altar": 3},
        "desired_x": 2,
        "mode": "x2_control",
    },
]

ASSERT_KEYS = ["A1_setup_ok", "A2_x_cap_overcounted", "A3_cast_x0_completes",
               "B1_setup_ok", "B2_x_cap_overcounted", "B3_x3_payment",
               "B3_x3_counters",
               "C1_setup_ok", "C2_x_cap_observed", "C3_cast_x2_completes"]


def setup_ready(g, state):
    cfg = g["cfg"]
    for name, n in cfg["need"].items():
        if len(battlefield_ids(state, 0, name)) < n:
            return False
    if cfg["spell"] not in hand_names(state, 0):
        return False
    if pool_total(state, 0) != 0:
        return False
    return state.get("phase") in ("PreCombatMain", "PostCombatMain") \
        and state.get("priority_player") == 0 \
        and len(state.get("stack", []) or []) == 0


async def p0_tick(g, st, acts, state):
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]

    def note(m):
        say(f"[{tag}] {m}")
        g["notes"].append(m)

    # ---- mulligan ----
    ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
    if ma and not g["kept"]:
        hn = hand_names(state, 0)
        mulls = g.get("mulls", 0)
        if cfg["spell"] in hn or mulls >= 4:
            g["kept"] = True
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            note(f"P0 keeps (spell in hand={cfg['spell'] in hn}, mulls={mulls})")
        else:
            g["mulls"] = mulls + 1
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            note(f"P0 mulligans #{mulls + 1}")
        return
    if (state.get("waiting_for") or {}).get("type") == "MulliganDecision":
        sc = next((a for a in acts if a["type"] == "SelectCards"), None)
        if sc and not g.get("bottomed"):
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == 0:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            hand_ids = [o for pl in state.get("players", [])
                        if pl.get("id") == 0 for o in pl.get("hand", [])]
            picks = sorted(
                hand_ids,
                key=lambda oid: 0 if obj_name(state, oid) == cfg["land"]
                else (2 if obj_name(state, oid) == cfg["spell"] else 1))[:count]
            g["bottomed"] = True
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            note(f"P0 bottoms {count} after mulligan")
        return

    # ---- generic auto-handlers ----
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire(f"{tag}_auto_pay", {"action": {k: v for k, v in a.items()
                                                if not k.startswith("_")}})
            await submit_as_is(c, a)
            return
    if (state.get("waiting_for") or {}).get("type") == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
        return
    if (state.get("waiting_for") or {}).get("type") == "DeclareBlockers":
        da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
        return
    if (state.get("waiting_for") or {}).get("type") == "DiscardToHandSize":
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                cands = data.get("candidates") or []
                if cands:
                    spec = data.get("spec") or {}
                    await submit_vi(c, opp, cands[0],
                                    seq=(opp.get("response", {})
                                         .get("type") != "exactChoices"))
                    note("P0 discards to hand size")
                    return
        return

    # ---- expected follow-up interactions ----
    if await handle_expected_interaction(g, st, state, note):
        return

    # ---- phase machine ----
    if g["phase"] == "setup":
        if (state.get("waiting_for") or {}).get("type") == "Priority" \
                and state.get("priority_player") == 0 \
                and state.get("active_player") == 0 \
                and state.get("phase") in ("PreCombatMain", "PostCombatMain"):
            if await setup_script(g, acts, state, note):
                return
        if (state.get("waiting_for") or {}).get("type") == "Priority" \
                and state.get("priority_player") == 0:
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    if g["phase"] == "assert":
        if not g["pre_exported"] and setup_ready(g, state):
            pre = await c.export_state()
            with open(f"{EVDIR}/pre_{tag}.json", "w") as f:
                f.write(pre)
            g["pre_exported"] = True
            g["pre_state"] = json.loads(pre)["state"]
            g["ass"][f"{tag}1_setup_ok"] = "passed"
            note(f"PRE exported; setup ready (turn {state.get('turn_number')})")
            wire(f"{tag}_pre_meta",
                 {"pool": mana_pool(state, 0),
                  "bf": sorted(Counter(
                      (o.get("base_name") or o.get("name"))
                      for o in state.get("objects", {}).values()
                      if o.get("zone") == "Battlefield"
                      and o.get("controller") == 0).items())})
        if g["pre_exported"]:
            offered, act = cast_spell_offered(state, st, cfg["spell"])
            if setup_ready(g, state):
                g["obs"] += 1
                if offered:
                    g["offered_seen"] = True
                    wire(f"{tag}_gate_offer_seen",
                         {"action": {k: v for k, v in act.items()
                                     if not k.startswith("_")}})
            if g["obs"] >= 4 or time.time() - g["assert_t0"] > 60:
                await finalize_assertion(g, note)
                return
        if (state.get("waiting_for") or {}).get("type") == "Priority" \
                and state.get("priority_player") == 0:
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    if g["phase"] == "act":
        if await act_tick(g, st, acts, state, note):
            return
        # hold priority while an expected interaction is pending
        if g.get("expecting") in ("xchoice", "sacrifice", "target"):
            if time.time() - g.get("expecting_t0", time.time()) > 60:
                note(f"expecting {g.get('expecting')} timed out; clearing")
                g["expecting"] = None
            else:
                return
        if (state.get("waiting_for") or {}).get("type") == "Priority" \
                and state.get("priority_player") == 0 \
                and not g.get("hold_priority"):
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    if (state.get("waiting_for") or {}).get("type") == "Priority" \
            and state.get("priority_player") == 0:
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return
    await vi_pass_fallback(g, c, st, note)


async def vi_pass_fallback(g, c, st, note):
    # never auto-pass while a manual payment observation is in progress
    if g.get("hold_priority"):
        return False
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    await submit_vi(c, opp, ch)
                    return True
    return False


async def setup_script(g, acts, state, note):
    c = g["p0"]
    cfg = g["cfg"]
    land = cfg["land"]
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "PlayLand" and obj_name(state, d.get("object_id")) == land:
            await submit_as_is(c, a)
            return True
    hn = hand_names(state, 0)
    if "Memnite" in hn and len(battlefield_ids(state, 0, "Memnite")) < \
            cfg["need"].get("Memnite", 0):
        return await cast_named(g, acts, state, "Memnite", note)
    for prod, cost in cfg["cast_cost"].items():
        if prod in hn and len(battlefield_ids(state, 0, prod)) < \
                cfg["need"].get(prod, 0) \
                and len(untapped_lands(state, 0, land)) >= cost:
            return await cast_named(g, acts, state, prod, note)
    if setup_ready(g, state):
        g["phase"] = "assert"
        g["assert_t0"] = time.time()
        note("setup complete -> assert phase")
        return False
    return False


async def cast_named(g, acts, state, name, note):
    c = g["p0"]
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == name:
            note(f"P0 casts {name}")
            wire(f"{g['cfg']['tag']}_cast_{name}", {"action": a})
            await submit_as_is(c, a)
            await asyncio.sleep(0.5)
            return True
    return False


async def cast_spell_action(g, st, state, note):
    c = g["p0"]
    cfg = g["cfg"]
    offered, act = cast_spell_offered(state, st, cfg["spell"])
    if not offered:
        return False
    if "_vi_choice" in act:
        opp, ch = act["_vi_opp"], act["_vi_choice"]
        note(f"casting {cfg['spell']} via viewer_interaction choice")
        wire(f"{cfg['tag']}_cast_vi", {"choice": choice_text(ch)[:160]})
        await submit_vi(c, opp, ch)
    else:
        note(f"casting {cfg['spell']}")
        wire(f"{cfg['tag']}_cast",
             {"action": {k: v for k, v in act.items()
                         if not k.startswith("_")}})
        await submit_as_is(c, act)
    # record the cast object's id so later assertions (e.g. A3) can tell the
    # resolved spell apart from discarded copies in the graveyard.
    try:
        g["cast_oid"] = (act.get("data", {}) or {}).get("object_id")
    except Exception:
        pass
    await asyncio.sleep(0.8)
    return True


async def finalize_assertion(g, note):
    cfg = g["cfg"]
    tag = cfg["tag"]
    seen = g["offered_seen"]
    note(f"gate: CastSpell({cfg['spell']}) offered_seen={seen} (obs={g['obs']})")
    wire(f"{tag}_gate_assert", {"offered_seen": seen, "obs": g["obs"]})
    g["phase"] = "act"
    g["act_step"] = 0
    g["act_t0"] = time.time()
    note("-> act phase")


# ---------------------------------------------------------------- interactions

async def handle_expected_interaction(g, st, state, note):
    """Answer xchoice / sacrifice / target prompts. Returns True if acted.

    Misclassification guard: only `schema`-type opportunities are treated as
    candidate prompts; exactChoices priority menus are never target prompts.
    """
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]
    expecting = g.get("expecting")
    if not expecting:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in g["answered"]:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        handled = False

        # xchoice: also served under "cast_flow" (Banefire asks for targets
        # before X, so the prompt order is not fixed). NOTE: on a spec
        # mismatch we must NOT `continue` the opportunity loop under
        # cast_flow -- the same opportunity may be the target prompt served
        # by the branch below.
        if expecting in ("xchoice", "cast_flow") and rtype == "schema":
            spec = data.get("spec", {}) or {}
            if spec.get("type") != "number" and expecting != "cast_flow":
                continue
            if spec.get("type") == "number":
                xchoice_matched = True
            else:
                xchoice_matched = False
        else:
            xchoice_matched = False
        if xchoice_matched:
            spec = data.get("spec", {}) or {}
            handled = True
            sdata = spec.get("data", {}) or {}
            xmin = sdata.get("min", spec.get("min"))
            xmax = sdata.get("max", spec.get("max"))
            g["x_max_observed"] = xmax
            bf_now = sorted(Counter(
                (o.get("base_name") or o.get("name") or "?")
                + ("(tapped)" if o.get("tapped") else "")
                for o in state.get("objects", {}).values()
                if o.get("zone") == "Battlefield"
                and o.get("controller") == 0).items())
            note(f"X-choice offered: min={xmin} max={xmax} "
                 f"(turn {state.get('turn_number')}, bf={bf_now})")
            wire(f"{tag}_xchoice",
                 {"min": xmin, "max": xmax, "interaction": opp,
                  "turn": state.get("turn_number"), "battlefield": bf_now,
                  "pool": mana_pool(state, 0)})
            with open(f"{EVDIR}/xchoice_{tag}.json", "w") as f:
                json.dump({"min": xmin, "max": xmax,
                           "interaction": opp}, f, indent=1, default=str)
            key = {"A": "A2_x_cap_overcounted",
                   "B": "B2_x_cap_overcounted",
                   "C": "C2_x_cap_observed"}[tag]

            def bf_count(prefix):
                return sum(c for name, c in bf_now
                           if name.startswith(prefix))

            n_mtn = bf_count("Mountain")
            n_kci = bf_count("Krark-Clan Ironworks")
            n_memnite = bf_count("Memnite")
            if tag == "A":
                # bounded-flow acceptance: mountains count legitimately; the
                # shared Memnite fuel bounds total KCI output at 2 per fuel
                # unit; minus the {R} fixed shard.
                acceptance = n_mtn + 2 * min(n_kci, n_memnite) - 1
            elif tag == "B":
                # shared single Memnite: KCI takes it (2), Altar gets 0.
                acceptance = n_mtn + 2
            else:
                acceptance = None
            g["x_acceptance"] = acceptance
            if acceptance is not None:
                over = xmax - acceptance
                g["ass"][key] = "passed" if over > 0 else "failed"
                note(f"{key}: max={xmax} vs bounded-flow acceptance "
                     f"{acceptance} (mtn={n_mtn} kci={n_kci} "
                     f"memnite={n_memnite}); delta={over:+d} "
                     f"-> {g['ass'][key]}")
                wire(f"{tag}_xcap_assert",
                     {"max": xmax, "acceptance": acceptance, "delta": over,
                      "n_mtn": n_mtn, "n_kci": n_kci, "n_memnite": n_memnite,
                      "result": g["ass"][key]})
            else:
                g["ass"][key] = f"observed_max={xmax}"
                note(f"{key}: max={xmax}")
            if xmin is None or xmax is None:
                note(f"X-choice min/max unparseable (min={xmin} max={xmax}); "
                     f"dumping and stalling for inspection")
                wire(f"{tag}_xchoice_unparseable", {"interaction": opp})
                g["answered"].add(iid)
                g["expecting"] = None
                g["x_unavailable"] = True
                g["phase"] = "done"
                return True
            desired = cfg["desired_x"]
            if not (xmin <= desired <= xmax):
                note(f"desired X={desired} outside [{xmin},{xmax}]; "
                     f"cannot proceed with planned commit")
                g["answered"].add(iid)
                g["expecting"] = None
                g["x_unavailable"] = True
                g["phase"] = "done"
                return True
            sub = await submit_number(c, opp, desired)
            g["answered"].add(iid)
            if expecting == "cast_flow":
                g["x_done"] = True
                if tag == "A":
                    # Banefire: target selection may come before or after X.
                    g["expecting"] = None if g.get("target_done") \
                        else "cast_flow"
                else:
                    g["expecting"] = "pay"
                    g["hold_priority"] = True
                    g["pay_t0"] = time.time()
            else:
                g["expecting"] = "target" if cfg["spell"] == "Banefire" \
                    else "pay"
                if g["expecting"] == "pay":
                    g["hold_priority"] = True
                    g["pay_t0"] = time.time()
            g["expecting_t0"] = time.time()
            note(f"chose X={desired} for {cfg['spell']}")
            wire(f"{tag}_x_sub", {"submission": sub, "x": desired})
            await asyncio.sleep(0.8)
            return True

        if expecting == "sacrifice" and rtype == "schema":
            handled = True
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            pick = None
            # prefer the configured fuel, else any own battlefield artifact
            for ch in avail:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    if isinstance(d, dict) and d.get("name") == "Memnite" \
                            and d.get("zone") == "battlefield":
                        pick = ch
                        break
                if pick:
                    break
            if pick is None:
                for ch in avail:
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        if isinstance(d, dict) \
                                and d.get("zone") == "battlefield":
                            pick = ch
                            break
                    if pick:
                        break
            if pick is None:
                note(f"sacrifice prompt: no usable candidate; "
                     f"texts={[choice_text(x)[:80] for x in avail]}")
                wire(f"{tag}_sacrifice_no_pick", {"interaction": opp})
                continue
            sub = await submit_vi(c, opp, pick, seq=True)
            g["answered"].add(iid)
            g["expecting"] = g.pop("_resume_expecting", None)
            g["expecting_t0"] = time.time()
            g["sacs_done"] = g.get("sacs_done", 0) + 1
            pname = next((s.get("data", {}).get("name")
                          for s in pick.get("surfaces", [])
                          if isinstance(s.get("data"), dict)
                          and s.get("data", {}).get("name")), "?")
            note(f"sacrificed (#{g['sacs_done']}): {pname}")
            g.setdefault("sac_names", []).append(pname)
            wire(f"{tag}_sacrifice_sub", {"submission": sub,
                                          "sacrificed": pname})
            await asyncio.sleep(0.8)
            return True

        if expecting in ("target", "cast_flow") and rtype == "schema":
            spec = data.get("spec", {}) or {}
            if spec.get("type") != "sequence":
                # e.g. an amount-assignment (DistributeAmong) prompt is not a
                # plain target pick; surface it instead of silently skipping.
                if iid not in g.get("dumped_iids", set()):
                    g.setdefault("dumped_iids", set()).add(iid)
                    wire(f"{tag}_target_unexpected_spec",
                         {"spec": spec, "interaction": opp})
                    note(f"target prompt has unexpected spec "
                         f"{spec.get('type')}; leaving for inspection")
                continue
            handled = True
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            pick = None
            for ch in avail:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    if isinstance(d, dict) and d.get("seat") == 1:
                        pick = ch
                        break
                if pick:
                    break
            if pick is None:
                note(f"target prompt: no seat-1 candidate; "
                     f"texts={[choice_text(x)[:80] for x in avail]}")
                wire(f"{tag}_target_no_pick", {"interaction": opp})
                continue
            sub = await submit_vi(c, opp, pick, seq=True)
            g["answered"].add(iid)
            if expecting == "cast_flow":
                g["target_done"] = True
                g["expecting"] = None if g.get("x_done") else "cast_flow"
            else:
                g["expecting"] = None
            note(f"targeted P1 for {cfg['spell']}")
            wire(f"{tag}_target_sub", {"submission": sub})
            await asyncio.sleep(0.8)
            return True

        if not handled and iid not in g.get("dumped_iids", set()):
            g.setdefault("dumped_iids", set()).add(iid)
            wire(f"{tag}_unmatched_interaction",
                 {"expecting": expecting,
                  "rtype": rtype, "interaction": opp})
            note(f"unmatched interaction while expecting={expecting}: "
                 f"rtype={rtype}")
    return False


async def activate_kci(g, state, acts, note):
    """Submit ActivateAbility for the first Krark-Clan Ironworks on board."""
    c = g["p0"]
    tag = g["cfg"]["tag"]
    kcis = battlefield_ids(state, 0, "Krark-Clan Ironworks")
    if not kcis:
        return False
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "ActivateAbility" and \
                (d.get("source_id") == kcis[0]
                 or str(a.get("_src_oid")) == str(kcis[0])):
            note("activating Krark-Clan Ironworks")
            wire(f"{tag}_activate_kci", {"action": {k: v for k, v in a.items()
                                                    if not k.startswith("_")}})
            await submit_as_is(c, a)
            g["_resume_expecting"] = g.get("expecting")
            g["expecting"] = "sacrifice"
            g["expecting_t0"] = time.time()
            await asyncio.sleep(0.5)
            return True
    return False


async def act_tick(g, st, acts, state, note):
    """Per-game act phase. Returns True if acted."""
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]
    step = g.get("act_step", 0)
    pool = mana_pool(state, 0)

    if time.time() - g.get("act_t0", time.time()) > 420:
        note("act phase timeout (420s)")
        g["phase"] = "done"
        return False

    my_prio = (state.get("waiting_for") or {}).get("type") == "Priority" \
        and state.get("priority_player") == 0

    if tag == "A":
        # step 0: submit the cast (target P1 and X=0 in either order).
        # step 1: await Banefire in graveyard; assert P1 still at 20.
        if step == 0 and my_prio:
            if g.get("expecting"):
                return False
            if await cast_spell_action(g, st, state, note):
                g["expecting"] = "cast_flow"
                g["x_done"] = False
                g["target_done"] = False
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            # the CAST Banefire (by object id) must reach the graveyard;
            # name matching alone would false-positive on discarded copies.
            cast_oid = g.get("cast_oid")
            gy_ids = [o for p in state.get("players", [])
                      if p.get("id") == 0 for o in p.get("graveyard", [])]
            if cast_oid is not None and cast_oid in gy_ids:
                ok = life(state, 1) == 20
                g["ass"]["A3_cast_x0_completes"] = "passed" if ok else "failed"
                note(f"A3: cast Banefire (oid {cast_oid}) resolved at X=0; "
                     f"P1 life={life(state, 1)} (want 20) -> "
                     f"{g['ass']['A3_cast_x0_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": graveyard_names(state, 0), "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A3_cast_x0_completes"] = "failed"
                note("A3: Banefire never reached graveyard in 120s")
                await export_post(g, note)
                g["phase"] = "done"
            return False

    if tag == "B":
        # step 0: submit the cast (xchoice -> X=3 -> manual pay).
        # step 1: good-faith manual payment; detect completion or stall.
        # step 2: payment done -- pass priority, let it resolve, record.
        if step == 0 and my_prio:
            if g.get("expecting"):
                return False
            if await cast_spell_action(g, st, state, note):
                g["expecting"] = "xchoice"
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            return await act_pay_b(g, st, acts, state, note)
        if step == 2:
            ones, spells = pay_status(state)
            if ones:
                n = plus_counters(state, ones[0])
                g["ass"]["B3_x3_counters"] = n
                note(f"B3: Endless One resolved with {n} +1/+1 counters "
                     f"(X was 3)")
                wire(f"{tag}_resolved", {"counters": n, "pool": pool})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            if g.get("resolve_passes", 0) >= 8 or \
                    time.time() - g.get("act_wait", time.time()) > 60:
                note(f"B3: paid spell not resolved after passes "
                     f"(stack_spells={len(spells)})")
                wire(f"{tag}_resolve_timeout",
                     {"stack_spells": len(spells)})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            g["resolve_passes"] = g.get("resolve_passes", 0) + 1
            return False  # fall through to priority passing

    if tag == "C":
        # step 0: submit the cast (xchoice -> X=2 -> manual pay).
        # step 1: KCI activation (sac Memnite -> {C}{C}); detect payment
        #         completion via the spell reaching the stack.
        # step 2: pass priority, let it resolve, assert 2 counters.
        if step == 0 and my_prio:
            if g.get("expecting"):
                return False
            if await cast_spell_action(g, st, state, note):
                g["expecting"] = "xchoice"
                g["expecting_t0"] = time.time()
                g["act_step"] = 1
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 1:
            if g.get("expecting") == "sacrifice":
                return False  # prompt is being answered
            ones, spells = pay_status(state)
            if ones or spells:
                g["hold_priority"] = False
                g["act_step"] = 2
                g["act_wait"] = time.time()
                g["resolve_passes"] = 0
                note(f"C: payment done (stack_spells={len(spells)}, "
                     f"ones_bf={len(ones)}); letting it resolve")
                wire(f"{tag}_pay_completed",
                     {"stack_spells": len(spells), "ones_bf": len(ones),
                      "pool": pool})
                return False
            if time.time() - g.get("act_wait", time.time()) > 60:
                g["ass"]["C3_cast_x2_completes"] = "failed"
                note("C3: payment never completed in 60s")
                wire(f"{tag}_pay_timeout", {"pool": pool})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            if g.get("sacs_done", 0) < 1:
                if await activate_kci(g, state, acts, note):
                    return True
            return False
        if step == 2:
            ones, spells = pay_status(state)
            if ones:
                n = plus_counters(state, ones[0])
                ok = n == 2
                g["ass"]["C3_cast_x2_completes"] = "passed" if ok else "failed"
                note(f"C3: Endless One on battlefield with {n} +1/+1 "
                     f"counters (want 2) -> {g['ass']['C3_cast_x2_completes']}")
                wire(f"{tag}_resolved",
                     {"counters": n, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            if g.get("resolve_passes", 0) >= 8 or \
                    time.time() - g.get("act_wait", time.time()) > 60:
                g["ass"]["C3_cast_x2_completes"] = "failed"
                note(f"C3: paid spell not resolved after passes "
                     f"(stack_spells={len(spells)})")
                wire(f"{tag}_resolve_timeout",
                     {"stack_spells": len(spells)})
                await export_post(g, note)
                g["phase"] = "done"
                return False
            g["resolve_passes"] = g.get("resolve_passes", 0) + 1
            return False  # fall through to priority passing
    return False


def pay_status(state):
    """Return (endless_one_ids, p0_stack_spells) for the pay phase."""
    ones = battlefield_ids(state, 0, "Endless One")
    spells = [e for e in (state.get("stack") or [])
              if e.get("controller") == 0
              and (e.get("kind") or {}).get("type") == "Spell"]
    return ones, spells


async def act_pay_b(g, st, acts, state, note):
    """Game B: good-faith manual payment for committed X=3. Never cancel.
    Returns True if acted.

    Payment completion is detected by the spell reaching the stack (payment
    done) or the battlefield (resolved) -- NOT by assuming a stall from a
    quiet viewer, since the engine may conclude the payment step without
    surfacing a PayMana prompt.
    """
    tag = g["cfg"]["tag"]
    pool = mana_pool(state, 0)
    if g.get("expecting") == "sacrifice":
        return False  # prompt is being answered
    if time.time() - g.get("pay_log_t", 0) > 10:
        g["pay_log_t"] = time.time()
        ones, spells = pay_status(state)
        note(f"pay telemetry: pool={pool}, stack_spells={len(spells)}, "
             f"ones_bf={len(ones)}, "
             f"waiting_for={(state.get('waiting_for') or {}).get('type')}, "
             f"pending_cast={bool(state.get('pending_cast'))}")
        wire(f"{tag}_pay_telemetry",
             {"pool": pool, "stack_spells": len(spells),
              "ones_bf": len(ones),
              "waiting_for": (state.get("waiting_for") or {}).get("type"),
              "pending_cast": bool(state.get("pending_cast"))})
    ones, spells = pay_status(state)
    if ones or spells:
        g["pay_done"] = True
        g["hold_priority"] = False
        cannibal = "Krark-Clan Ironworks" in (g.get("sac_names") or [])
        mode = ("completed_via_self_cannibalization"
                if cannibal else "completed")
        g["ass"]["B3_x3_payment"] = mode
        note(f"B3: X=3 payment {mode}: pool={pool}, "
             f"stack_spells={len(spells)}, ones_bf={len(ones)}, "
             f"sacs={g.get('sac_names')}")
        wire(f"{tag}_pay_completed",
             {"mode": mode, "pool": pool, "stack_spells": len(spells),
              "ones_bf": len(ones), "sacs": g.get("sac_names"),
              "gy": graveyard_names(state, 0)})
        try:
            post = await g["p0"].export_state()
            with open(f"{EVDIR}/midpay_{tag}.json", "w") as f:
                f.write(post)
            note("mid-pay state exported")
        except Exception as e:
            note(f"mid-pay export failed: {e}")
        g["act_step"] = 2  # let the stack resolve, then record counters
        g["act_wait"] = time.time()
        g["resolve_passes"] = 0
        return False
    if time.time() - g.get("pay_t0", time.time()) > 60:
        kci_acts = [a for a in acts
                    if a["type"] == "ActivateAbility"
                    and "Krark-Clan Ironworks" in
                    obj_name(state, a.get("data", {}).get("source_id")
                             or a.get("_src_oid"))]
        altar_acts = [a for a in acts
                      if a["type"] == "ActivateAbility"
                      and "Phyrexian Altar" in
                      obj_name(state, a.get("data", {}).get("source_id")
                               or a.get("_src_oid"))]
        g["ass"]["B3_x3_payment"] = "stalled_unpayable"
        note(f"B3: X=3 payment STALLED after 60s: pool={pool}, "
             f"gy={graveyard_names(state, 0)}, "
             f"KCI activations offered={len(kci_acts)}, "
             f"Altar activations offered={len(altar_acts)}, "
             f"waiting_for={(state.get('waiting_for') or {}).get('type')}")
        wire(f"{tag}_pay_stalled",
             {"pool": pool, "gy": graveyard_names(state, 0),
              "kci_acts": len(kci_acts), "altar_acts": len(altar_acts),
              "waiting_for": (state.get("waiting_for") or {}).get("type"),
              "pending_cast": bool(state.get("pending_cast"))})
        g["hold_priority"] = False
        await export_post(g, note)
        g["phase"] = "done"
        return False
    # good-faith attempt: up to 2 KCI activations (fuel, then self/altar)
    if g.get("sacs_done", 0) < 2:
        if await activate_kci(g, state, acts, note):
            return True
    return False


async def export_post(g, note):
    c = g["p0"]
    tag = g["cfg"]["tag"]
    try:
        post = await c.export_state()
        with open(f"{EVDIR}/post_{tag}.json", "w") as f:
            f.write(post)
        g["post_exported"] = True
        note("POST exported")
    except Exception as e:
        note(f"post export failed: {e}")


# ---------------------------------------------------------------- P1 + runner

async def p1_tick(g, st, acts, state):
    c = g["p1"]
    wtype = (state.get("waiting_for") or {}).get("type")
    ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
    if ma and not g.get("p1kept"):
        g["p1kept"] = True
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        return
    if wtype == "MulliganDecision":
        return
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
        return
    if wtype == "DeclareBlockers":
        da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
        return
    if wtype != "Priority" or state.get("priority_player") != 1:
        return
    for a in acts:
        if a["type"] == "PlayLand":
            await submit_as_is(c, a)
            return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return
    await vi_pass_fallback(g, c, st, lambda m: None)


async def run_game(cfg):
    tag = cfg["tag"]
    say(f"===== GAME {tag}: {cfg['spell']} {cfg['cost']} (X={cfg['desired_x']}) =====")
    wire("game_start", {"tag": tag, "cfg": cfg})
    g = {"cfg": cfg, "phase": "setup", "notes": [],
         "ass": {}, "answered": set(), "obs": 0, "offered_seen": False,
         "pre_exported": False, "post_exported": False,
         "kept": False, "expecting": None, "sacs_done": 0,
         "x_max_observed": None, "hold_priority": False}
    t_start = time.time()
    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(*cfg["deck"]))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    g["p0"], g["p1"] = p0, p1
    g["game_code"] = p0.game_code
    say(f"[{tag}] game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    last = {}
    last_tick_at = {}
    try:
        while time.time() - t_start < 560:
            await asyncio.sleep(0.15)
            for c, tick in ((p0, p0_tick), (p1, p1_tick)):
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                # Liveness: re-tick at most every 5s even with no revision
                # change (a tick that sends nothing leaves no revision change;
                # without this the loop can deadlock on a priority owner).
                if rev == last.get(c.name) and \
                        time.time() - last_tick_at.get(c.name, 0) <= 5:
                    continue
                last[c.name] = rev
                last_tick_at[c.name] = time.time()
                try:
                    await tick(g, st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: {e}")
                    wire(f"{tag}_tick_error", {"who": c.name, "err": str(e)})
            if g["phase"] == "done":
                break
        else:
            g["notes"].append("per-game timeout (560s) hit")
            say(f"[{tag}] TIMEOUT")
    finally:
        for key, fname in (("pre", f"pre_{tag}.json"),
                           ("post", f"post_{tag}.json")):
            if not g.get(f"{key}_exported"):
                try:
                    exp = await p0.export_state()
                    with open(f"{EVDIR}/{fname}", "w") as f:
                        f.write(exp)
                    g[f"{key}_exported"] = True
                    say(f"[{tag}] exported {fname} at teardown")
                except Exception as e:
                    g["notes"].append(f"{key} export at teardown failed: {e}")
        try:
            await p0.close()
        except Exception:
            pass
        try:
            await p1.close()
        except Exception:
            pass
    g["duration_s"] = round(time.time() - t_start, 1)
    return g


async def main():
    t_start = time.time()
    games = {}
    only = os.environ.get("ONLY_GAME")
    cfgs = [c for c in GAMES if not only or c["tag"] == only]
    for cfg in cfgs:
        games[cfg["tag"]] = await run_game(cfg)

    ass = {k: "not-run" for k in ASSERT_KEYS}
    for tag, g in games.items():
        for k, v in g["ass"].items():
            if k in ass:
                ass[k] = v

    notes_all = []
    for tag, g in games.items():
        notes_all.append(f"--- game {tag} ({g['cfg']['spell']} {g['cfg']['cost']}, "
                         f"X={g['cfg']['desired_x']}) duration {g['duration_s']}s; "
                         f"x_max_observed={g['x_max_observed']}; "
                         f"offered_seen={g['offered_seen']} ---")
        notes_all.extend(g["notes"])

    a2 = games.get("A", {}).get("x_max_observed")
    b2 = games.get("B", {}).get("x_max_observed")
    a2_over = games.get("A", {}).get("ass", {}).get("A2_x_cap_overcounted")
    b2_over = games.get("B", {}).get("ass", {}).get("B2_x_cap_overcounted")
    present = [t for t in "ABC" if t in games]
    setups_ok = bool(present) and all(
        games[t]["ass"].get(f"{t}1_setup_ok") == "passed" for t in present)
    if setups_ok and (a2_over == "passed" or b2_over == "passed"):
        verdict = "reproduced"
    elif setups_ok and a2_over == "failed" and b2_over == "failed":
        verdict = "not-reproduced"
    elif not setups_ok:
        verdict = "blocked"
        notes_all.append("setup incomplete for at least one game; see notes")
    else:
        verdict = "blocked"
        notes_all.append(f"inconclusive X-max combination (A={a2}, B={b2}); "
                         "see notes")

    result_line = (
        f"A1 setup_ok: {ass['A1_setup_ok']}; "
        f"A2 x_cap_overcounted: {ass['A2_x_cap_overcounted']} "
        f"(observed max={a2}, acceptance={games.get('A', {}).get('x_acceptance')}); "
        f"A3 cast_x0_completes: {ass['A3_cast_x0_completes']}; "
        f"B1 setup_ok: {ass['B1_setup_ok']}; "
        f"B2 x_cap_overcounted: {ass['B2_x_cap_overcounted']} "
        f"(observed max={b2}, acceptance={games.get('B', {}).get('x_acceptance')}); "
        f"B3 x3_payment: {ass['B3_x3_payment']}; "
        f"B3 x3_counters: {ass.get('B3_x3_counters', 'n/a')}; "
        f"C1 setup_ok: {ass['C1_setup_ok']}; "
        f"C2 x_cap_observed: {ass['C2_x_cap_observed']}; "
        f"C3 cast_x2_completes: {ass['C3_cast_x2_completes']}"
    )

    run = {
        "issue": 1235,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(time.time() - t_start, 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_1235.py", "rb").read()).hexdigest(),
        "games": {tag: {
            "spell": g["cfg"]["spell"], "cost": g["cfg"]["cost"],
            "desired_x": g["cfg"]["desired_x"],
            "deck": g["cfg"]["deck"], "game_code": g.get("game_code"),
            "duration_s": g["duration_s"],
            "x_max_observed": g["x_max_observed"],
            "x_acceptance": g.get("x_acceptance"),
            "assertions": g["ass"],
            "offered_seen": g["offered_seen"], "obs": g["obs"],
        } for tag, g in games.items()},
        "assertions": ass,
        "notes": notes_all,
        "verdict": verdict,
        "result": result_line,
        "scope": "feasible_mana_capacity per-permanent sum over-count in "
                 "chain-sacrifice configurations (Krark-Clan Ironworks / "
                 "Phyrexian Altar); X-choice advertised max vs the issue's "
                 "acceptance caps; manual-payment outcome for an over-counted "
                 "commit. Native engine, two human-client seats.",
        "limitations": [
            "Browser UI not exercised.",
            "Dense playsets (>4-of) are a test-harness convenience; the "
            "engine accepts them for custom games.",
        ],
        "setup_line": ("A: 2x Krark-Clan Ironworks + Mountain + Memnite, "
                       "Banefire in hand. B/C: KCI + Phyrexian Altar + "
                       "Memnite, Endless One in hand. Empty pool, own main "
                       "phase."),
        "contract_line": ("A: X-choice max must be 4 iff bug present "
                          "(acceptance: 2); X=0 cast completes. B: X-choice "
                          "max must be 3 iff bug present (acceptance: 2); "
                          "commit X=3, good-faith manual payment, record "
                          "completion or stall. C: commit X=2, KCI sac "
                          "Memnite pays {2}, Endless One enters with 2 "
                          "counters."),
        "stats": {"states_seen": "see wire_log.jsonl",
                  "trigger_observations": "see wire_log.jsonl"},
        "contract": (
            "Game A ({X}{R} Banefire, 2 KCI + Mountains + Memnite): X chooser "
            "max == 4 records the over-count (acceptance: 2); then X=0 cast "
            "completes as a control. Game B ({X} Endless One, KCI + Altar + "
            "Memnite): X chooser max == 3 records the over-count "
            "(acceptance: 2); then X=3 is committed and paid manually in "
            "good faith (KCI activations, auto PayMana, never cancel) to "
            "record completion or an unpayable stall. Game C (same board): "
            "X=2 commits; KCI sac of Memnite pays {2}; Endless One must "
            "enter with exactly 2 +1/+1 counters."
        ),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{BACKFILL}/driver/scenario_1235.py", "rb") as src, \
            open(f"{EVDIR}/scenario_1235.py", "wb") as dst:
        dst.write(src.read())
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
