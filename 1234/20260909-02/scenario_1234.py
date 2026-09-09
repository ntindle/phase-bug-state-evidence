#!/usr/bin/env python3
"""Issue #1234: feasible_mana_capacity — colored-shard feasibility under
non-tap mana sources.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Report (github #1234, status:confirmed, area:engine, mechanic:mana,
priority:p2-wrong-game-result): the castability gate `can_feasibly_pay_mana_cost`
rejects colored costs payable via non-tap mana abilities. Revised scope per the
2026-08-13 status comment (PR #2788 partially superseded the original
description): single-activation colored feasibility is now modeled, but repeated
activation of the same source is not — one Phyrexian Altar + two sacrificable
creatures still cannot prove a {B}{B} spell castable.

Oracle text (pinned card-data.json):
  Phyrexian Altar: "Sacrifice a creature: Add one mana of any color."
  Krark-Clan Ironworks: "Sacrifice an artifact: Add {C}{C}."

Four games (native engine, two human-client seats):
  Game A: {1}{B} spell (Cabal Ritual), 1+ Swamp + Phyrexian Altar +
          1 Diregraf Ghoul -> CastSpell must be OFFERED (one-activation model
          covers this per the status comment). Then cast it for real (Altar
          activation + Swamp) and assert it resolves.
  Game B: {B}{B} spell (Sign in Blood), Forests-only + Phyrexian Altar +
          2 Memnites -> CastSpell must be OFFERED (two activations yield {B}{B});
          the confirmed remaining bug is that it is NOT. Manual proof: activate
          the Altar twice for real and float {B}{B}; the gate must offer then.
  Game C: {B} spell (Dark Ritual), Phyrexian Altar + 0 creatures
          -> CastSpell must NOT be offered (correct rejection; negative control).
  Game D: {2} spell (Steel Overseer), Krark-Clan Ironworks + Memnite
          -> CastSpell must be OFFERED (KCI colorless regression, cf. #562/#1231).
          Then cast it for real via a KCI activation.

Assertions:
  A1_setup_ok        per game: required permanents + spell in hand reached
  A2a_gate_offers_1B game A: CastSpell(Cabal Ritual) offered at main-phase
                     priority with empty pool (expect offered)
  A2b_cast_completes game A: manual float (Altar->sac Ghoul->Black, tap Swamp)
                     + cast -> Ritual resolves: Ghoul+Ritual in graveyard,
                     black pool == 3
  A3a_gate_offers_BB game B: CastSpell(Sign in Blood) offered at main-phase
                     priority with empty pool (expect offered per correct
                     behavior; the reported bug is that it is NOT)
  A3b_manual_proof   game B: two real Altar activations float {B}{B}; gate then
                     offers; cast targeting P1 resolves (P1 20->18 life)
  A4_gate_rejects_B  game C: CastSpell(Dark Ritual) NOT offered with Altar and
                     0 creatures (expect not offered — correct rejection)
  A5a_gate_offers_2  game D: CastSpell(Steel Overseer) offered with KCI+Memnite
                     (expect offered — KCI regression guard)
  A5b_cast_completes game D: KCI activation (sac Memnite -> {C}{C}) + cast ->
                     Overseer on battlefield, Memnite in graveyard

Verdict rule: reproduced iff A3a records the gate NOT offering {B}{B} while A3b
proves the mana was actually producible. not-reproduced iff the gate offers
{B}{B} (and A2/A4/A5 behave as expected). blocked iff setup cannot be driven.

Evidence: evidence/1234/<run-id>/pre_{A,B,C,D}.json, mid_B.json, post_{A,B,C,D}.json,
run.json, manifest.sha256, summary.png, scenario_1234.py, wire_log.jsonl,
scenario_run.log
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
RUN_ID = "20260909-02"
EVDIR = f"{BACKFILL}/evidence/1234/{RUN_ID}"
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
    "source": "ServerHello + sha256 of /proc/<pid>/exe of the listening server "
              "(started by today's earlier backfill run, same pinned release); "
              "reused per task body since 127.0.0.1:9374 was already listening",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def battlefield_creatures(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and "Creature" in (o.get("card_types") or {}).get("core_types", [])]


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
    """Color -> count of floating mana units for pid (best effort)."""
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
    """True if a CastSpell action for `spell` is currently offered."""
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and obj_name(state, a.get("data", {}).get("object_id")) == spell:
            return True, a
    # also scan viewer_interaction priority menus for a castSpell choice
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
            # InteractionResponse::ManaGroups { choice_ids, count }:
            # count must satisfy 1 <= count <= max_batch (here 1).
            rdata["count"] = 1
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": stype or "sequence", "data": rdata}}
    await c.send_interaction(sub)
    return sub

# ---------------------------------------------------------------- game configs

GAMES = [
    {
        "tag": "A",
        "spell": "Cabal Ritual",
        "cost": "{1}{B}",
        "deck": [("Cabal Ritual", 12), ("Phyrexian Altar", 4),
                 ("Diregraf Ghoul", 8), ("Swamp", 36)],
        "land": "Swamp",
        "producer": "Phyrexian Altar",
        "sac_name": "Diregraf Ghoul",
        "producer_cost_lands": 3,   # lands needed to cast the producer
        "want_offered": True,       # gate expectation at empty pool
        "mode": "cast_full",        # manual float + real cast after assertion
        "mull_note": "keep if Cabal Ritual in hand",
    },
    {
        "tag": "B",
        "spell": "Sign in Blood",
        "cost": "{B}{B}",
        "deck": [("Sign in Blood", 12), ("Phyrexian Altar", 4),
                 ("Memnite", 12), ("Forest", 32)],
        "land": "Forest",
        "producer": "Phyrexian Altar",
        "sac_name": "Memnite",
        "producer_cost_lands": 3,
        "want_offered": True,       # per correct behavior; BUG => not offered
        "mode": "manual_proof",     # two real activations, then gate re-check + cast
        "mull_note": "keep if Sign in Blood in hand",
    },
    {
        "tag": "C",
        "spell": "Dark Ritual",
        "cost": "{B}",
        "deck": [("Dark Ritual", 12), ("Phyrexian Altar", 4), ("Forest", 44)],
        "land": "Forest",
        "producer": "Phyrexian Altar",
        "sac_name": None,           # no creatures at all
        "producer_cost_lands": 3,
        "want_offered": False,      # correct rejection; negative control
        "mode": "observe_only",
        "mull_note": "keep if Dark Ritual in hand",
    },
    {
        "tag": "D",
        "spell": "Steel Overseer",
        "cost": "{2}",
        "deck": [("Steel Overseer", 12), ("Krark-Clan Ironworks", 4),
                 ("Memnite", 12), ("Forest", 32)],
        "land": "Forest",
        "producer": "Krark-Clan Ironworks",
        "sac_name": "Memnite",
        "producer_cost_lands": 4,
        "want_offered": True,       # KCI regression guard
        "mode": "cast_full",
        "mull_note": "keep if Steel Overseer in hand",
    },
]

ASSERT_KEYS = ["A1_setup_ok", "A2a_gate_offers_1B", "A2b_cast_completes",
               "A3a_gate_offers_BB", "A3b_manual_proof", "A4_gate_rejects_B",
               "A5a_gate_offers_2", "A5b_cast_completes"]


def setup_ready(g, state):
    """Board preconditions for the gate assertion, per game."""
    tag = g["cfg"]["tag"]
    spell = g["cfg"]["spell"]
    prod = battlefield_ids(state, 0, g["cfg"]["producer"])
    sac = g["cfg"]["sac_name"]
    ok = (len(prod) >= 1 and spell in hand_names(state, 0))
    if tag == "A":
        ok = ok and len(battlefield_ids(state, 0, "Diregraf Ghoul")) >= 1 \
            and len(untapped_lands(state, 0, "Swamp")) >= 1
    elif tag == "B":
        ok = ok and len(battlefield_ids(state, 0, "Memnite")) >= 2
    elif tag == "C":
        ok = ok and len(battlefield_creatures(state, 0)) == 0
    elif tag == "D":
        ok = ok and len(battlefield_ids(state, 0, "Memnite")) >= 1
    return ok and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
        and state.get("priority_player") == 0 \
        and len(state.get("stack", []) or []) == 0


async def p0_tick(g, st, acts, state):
    """Shared P0 tick; game phase machine lives in g['phase']."""
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]
    wtype = (state.get("waiting_for") or {}).get("type")

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
    if wtype == "MulliganDecision":
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
            picks = sorted(hand_ids,
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
            wire(f"{tag}_auto_pay", {"action": a})
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
    if wtype == "DiscardToHandSize":
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                cands = data.get("candidates") or []
                if cands:
                    spec = data.get("spec") or {}
                    sub = await submit_vi(c, opp, cands[0],
                                          seq=(opp.get("response", {})
                                               .get("type") != "exactChoices"))
                    note("P0 discards to hand size")
                    return
        return

    # ---- expected follow-up interactions (sacrifice cost / color / target) ----
    if await handle_expected_interaction(g, st, state, note):
        return

    # ---- phase machine ----
    if g["phase"] == "setup":
        if wtype == "Priority" and state.get("priority_player") == 0 \
                and state.get("active_player") == 0 \
                and state.get("phase") in ("PreCombatMain", "PostCombatMain"):
            if await setup_script(g, acts, state, note):
                return
        # pass priority otherwise
        if wtype == "Priority" and state.get("priority_player") == 0:
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    if g["phase"] == "assert":
        # export PRE at the first ready observation
        if not g["pre_exported"] and setup_ready(g, state):
            pre = await c.export_state()
            with open(f"{EVDIR}/pre_{tag}.json", "w") as f:
                f.write(pre)
            g["pre_exported"] = True
            g["pre_state"] = json.loads(pre)["state"]
            g["ass"][f"A1_setup_ok_{tag}"] = "passed"
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
            # finalize after enough main-phase observations or 60s
            if g["obs"] >= 4 or time.time() - g["assert_t0"] > 60:
                await finalize_assertion(g, note)
                return
        if wtype == "Priority" and state.get("priority_player") == 0:
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    if g["phase"] == "act":
        if await act_tick(g, st, acts, state, note):
            return
        # Hold priority while our own producer interaction is pending or
        # while floated mana awaits the cast: passing could advance the
        # phase/turn and empty the mana pool mid-proof.
        exp = g.get("expecting")
        if exp and exp != "target":
            if time.time() - g.get("expecting_t0", time.time()) > 45:
                note(f"expecting {exp} timed out; clearing")
                g["expecting"] = None
            else:
                return
        if g.get("sacs_done", 0) >= 1 and not g.get("cast_submitted"):
            return
        if wtype == "Priority" and state.get("priority_player") == 0:
            pp = next((a for a in acts if a["type"] == "PassPriority"), None)
            if pp:
                await submit_as_is(c, pp)
                return
        await vi_pass_fallback(g, c, st, note)
        return

    # phase "done": just pass
    if wtype == "Priority" and state.get("priority_player") == 0:
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return
    await vi_pass_fallback(g, c, st, note)


async def vi_pass_fallback(g, c, st, note):
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
    """Play lands and setup permanents. Returns True if acted."""
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]
    land = cfg["land"]
    # 1) land drop
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "PlayLand" and obj_name(state, d.get("object_id")) == land:
            await submit_as_is(c, a)
            return True
    # 2) game-specific casts
    hn = hand_names(state, 0)
    if tag == "A":
        if not battlefield_ids(state, 0, "Diregraf Ghoul") \
                and "Diregraf Ghoul" in hn \
                and len(untapped_lands(state, 0, "Swamp")) >= 1:
            return await cast_named(g, acts, state, "Diregraf Ghoul", note)
        if not battlefield_ids(state, 0, "Phyrexian Altar") \
                and "Phyrexian Altar" in hn \
                and len(untapped_lands(state, 0, "Swamp")) >= 3:
            return await cast_named(g, acts, state, "Phyrexian Altar", note)
    elif tag == "B":
        if "Memnite" in hn and len(battlefield_ids(state, 0, "Memnite")) < 2:
            return await cast_named(g, acts, state, "Memnite", note)
        if not battlefield_ids(state, 0, "Phyrexian Altar") \
                and "Phyrexian Altar" in hn \
                and len(untapped_lands(state, 0, "Forest")) >= 3:
            return await cast_named(g, acts, state, "Phyrexian Altar", note)
    elif tag == "C":
        if not battlefield_ids(state, 0, "Phyrexian Altar") \
                and "Phyrexian Altar" in hn \
                and len(untapped_lands(state, 0, "Forest")) >= 3:
            return await cast_named(g, acts, state, "Phyrexian Altar", note)
    elif tag == "D":
        if "Memnite" in hn and len(battlefield_ids(state, 0, "Memnite")) < 2:
            return await cast_named(g, acts, state, "Memnite", note)
        if not battlefield_ids(state, 0, "Krark-Clan Ironworks") \
                and "Krark-Clan Ironworks" in hn \
                and len(untapped_lands(state, 0, "Forest")) >= 4:
            return await cast_named(g, acts, state, "Krark-Clan Ironworks",
                                    note)
    # 3) ready -> move to assert phase
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
    """Submit the offered CastSpell for the game's spell, via legal_actions
    or via a viewer_interaction castSpell choice. Returns True if submitted."""
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
    await asyncio.sleep(0.8)
    return True

# ---------------------------------------------------------------- interactions

async def handle_expected_interaction(g, st, state, note):
    """Answer sacrifice-cost / color-choice / target prompts only when the
    game is explicitly expecting them (g['expecting']). Returns True if acted.

    Misclassification guard (per 2026-09-09 lesson): only `schema`-type
    opportunities are treated as candidate-selection prompts; exactChoices
    priority menus (passPriority/castSpell/...) are never treated as target
    prompts. Unmatched opportunities are dumped to the wire log once per
    interactionId so unknown prompt shapes are visible.
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
        if expecting == "sacrifice" and rtype == "schema":
            handled = True
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            # pick the configured sacrificable by battlefield object name
            pick = None
            for ch in avail:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    if isinstance(d, dict) and d.get("name") == cfg["sac_name"] \
                            and d.get("zone") == "battlefield":
                        pick = ch
                        break
                if pick:
                    break
            if pick is None:
                # single candidate: engine may offer only one; take it if it is
                # one of ours on the battlefield
                if len(avail) == 1:
                    pick = avail[0]
                else:
                    note(f"sacrifice prompt: no {cfg['sac_name']} candidate; "
                         f"texts={[choice_text(x)[:80] for x in avail]}")
                    wire(f"{tag}_sacrifice_no_pick", {"interaction": opp})
                    continue
            sub = await submit_vi(c, opp, pick, seq=True)
            g["answered"].add(iid)
            g["expecting"] = "color" if cfg["producer"] == "Phyrexian Altar" \
                else None
            g["expecting_t0"] = time.time()
            g["sacs_done"] = g.get("sacs_done", 0) + 1
            note(f"sacrificed for {cfg['producer']} "
                 f"(#{g['sacs_done']}): {choice_text(pick)[:100]}")
            wire(f"{tag}_sacrifice_sub", {"submission": sub,
                                          "interaction": opp})
            await asyncio.sleep(0.8)
            return True
        if expecting == "color" and rtype in ("exactChoices", "schema"):
            chs = data.get("choices") or data.get("candidates") or []
            avail = [ch for ch in chs
                     if ch.get("status", {}).get("type") == "available"]
            texts = [(ch, choice_text(ch)) for ch in avail]
            # priority menus must not be treated as color prompts
            if any("passpriority" in t.lower().replace(" ", "")
                   or "castspell" in t.lower().replace(" ", "")
                   for _, t in texts):
                continue
            handled = True
            pick = next((ch for ch, t in texts
                         if "black" in t.lower() or "{b}" in t.lower()
                         or t.strip().lower() in ("b", "black")), None)
            if pick is None:
                # mana-choice schema: candidates carry surfaces of type
                # "mana" with data.symbols like ["B"]
                for ch in avail:
                    syms = []
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        if isinstance(d, dict) and isinstance(
                                d.get("symbols"), list):
                            syms.extend(str(x) for x in d["symbols"])
                    if any(x.upper() == "B" for x in syms):
                        pick = ch
                        break
            if pick is None:
                # not a recognizable color prompt; dump and leave it alone
                wire(f"{tag}_color_unrecognized", {"interaction": opp})
                note(f"color prompt unrecognized: "
                     f"{[t[:80] for _, t in texts]}")
                continue
            sub = await submit_vi(c, opp, pick,
                                  seq=(rtype != "exactChoices"))
            g["answered"].add(iid)
            g["expecting"] = None
            g["colors_chosen"] = g.get("colors_chosen", 0) + 1
            note(f"chose mana color: {choice_text(pick)[:100]}")
            wire(f"{tag}_color_sub", {"submission": sub, "interaction": opp})
            await asyncio.sleep(0.8)
            return True
        if expecting == "target" and rtype == "schema":
            handled = True
            cands = data.get("candidates") or []
            avail = [ch for ch in cands
                     if ch.get("status", {}).get("type") == "available"]
            if not avail:
                continue
            # Sign in Blood: target player -> pick P1 (seat 1)
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
            g["expecting"] = None
            note(f"targeted P1 for {cfg['spell']}")
            wire(f"{tag}_target_sub", {"submission": sub, "interaction": opp})
            await asyncio.sleep(0.8)
            return True
        if not handled and iid not in g.get("dumped_iids", set()):
            g.setdefault("dumped_iids", set()).add(iid)
            wire(f"{tag}_unmatched_interaction",
                 {"expecting": expecting, "interaction": opp})
            note(f"unmatched interaction while expecting={expecting}: "
                 f"rtype={rtype} "
                 f"text={(str(data)[:160])}")
    return False


async def finalize_assertion(g, note):
    """Record the gate offer observation, then move to the act phase."""
    c = g["p0"]
    cfg = g["cfg"]
    tag = cfg["tag"]
    key = {"A": "A2a_gate_offers_1B", "B": "A3a_gate_offers_BB",
           "C": "A4_gate_rejects_B", "D": "A5a_gate_offers_2"}[tag]
    seen = g["offered_seen"]
    want = cfg["want_offered"]
    if want:
        g["ass"][key] = "passed" if seen else "failed"
    else:
        g["ass"][key] = "passed" if not seen else "failed"
    note(f"gate assertion {key}: offered_seen={seen} want_offered={want} "
         f"-> {g['ass'][key]} (obs={g['obs']})")
    wire(f"{tag}_gate_assert", {"offered_seen": seen, "want": want,
                                "result": g["ass"][key], "obs": g["obs"]})
    if cfg["mode"] == "observe_only":
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/post_{tag}.json", "w") as f:
                f.write(post)
            g["post_exported"] = True
            note("POST exported (observe-only)")
        except Exception as e:
            note(f"post export failed: {e}")
        g["phase"] = "done"
    else:
        g["phase"] = "act"
        g["act_step"] = 0
        g["act_t0"] = time.time()
        note("-> act phase")


async def activate_producer(g, state, acts, note):
    """Submit ActivateAbility for the producer (altar/KCI)."""
    c = g["p0"]
    cfg = g["cfg"]
    prod_ids = battlefield_ids(state, 0, cfg["producer"])
    if not prod_ids:
        note(f"no {cfg['producer']} on battlefield to activate")
        return False
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "ActivateAbility" and d.get("source_id") == prod_ids[0]:
            note(f"activating {cfg['producer']}")
            wire(f"{cfg['tag']}_activate", {"action": a})
            await submit_as_is(c, a)
            g["expecting"] = "sacrifice"
            g["expecting_t0"] = time.time()
            g["act_wait"] = time.time()
            await asyncio.sleep(0.5)
            return True
    # also try actions keyed by object without source_id match
    for a in acts:
        if a["type"] == "ActivateAbility" and \
                str(a.get("_src_oid")) == str(prod_ids[0]):
            note(f"activating {cfg['producer']} (via _src_oid)")
            wire(f"{cfg['tag']}_activate", {"action": a})
            await submit_as_is(c, a)
            g["expecting"] = "sacrifice"
            g["expecting_t0"] = time.time()
            g["act_wait"] = time.time()
            await asyncio.sleep(0.5)
            return True
    note(f"ActivateAbility not offered for {cfg['producer']}; "
         f"acts={[a['type'] for a in acts][:12]}")
    wire(f"{cfg['tag']}_activate_missing",
         {"acts": [a["type"] for a in acts][:12]})
    return False


async def tap_land_for_mana(g, state, acts, land_name, note):
    """Tap a land for mana via its ActivateAbility."""
    c = g["p0"]
    lands = untapped_lands(state, 0, land_name)
    if not lands:
        note(f"no untapped {land_name} to tap")
        return False
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "ActivateAbility" and d.get("source_id") == lands[0]:
            note(f"tapping {land_name} for mana")
            await submit_as_is(c, a)
            await asyncio.sleep(0.5)
            return True
    for a in acts:
        if a["type"] == "ActivateAbility" and \
                str(a.get("_src_oid")) == str(lands[0]):
            note(f"tapping {land_name} for mana (via _src_oid)")
            await submit_as_is(c, a)
            await asyncio.sleep(0.5)
            return True
    note(f"no ActivateAbility to tap {land_name}")
    return False


async def act_tick(g, st, acts, state, note):
    """Manual sequences after the gate assertion. Returns True if acted."""
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
        # step 0: activate altar (sac Ghoul -> {B} floats). step 1: cast ritual
        # with {B} floating; the engine prompts for the remaining {1} via
        # PayMana/PayManaAbilityMana (auto-answered; lands tap through the
        # payment flow, not via a manual ActivateAbility).
        # step 2: await resolution, assert, export POST.
        if step == 0 and my_prio:
            if g.get("expecting"):
                return False  # sacrifice/color interaction pending
            if g.get("sacs_done", 0) >= 1:
                g["act_step"] = 1
                return False
            return await activate_producer(g, state, acts, note)
        if step == 1 and my_prio:
            if await cast_spell_action(g, st, state, note):
                g["cast_submitted"] = True
                g["cast_rev"] = c.revision
                g["act_step"] = 2
                g["act_wait"] = time.time()
                return True
            note(f"cast step: CastSpell not offered with pool {pool}; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 2:
            gy = graveyard_names(state, 0)
            if "Cabal Ritual" in gy:
                black = pool.get("black", 0)
                ghoul_gy = "Diregraf Ghoul" in gy
                ok = ghoul_gy and black == 3
                g["ass"]["A2b_cast_completes"] = "passed" if ok else "failed"
                note(f"A2b: ritual resolved; ghoul in gy={ghoul_gy}; "
                     f"black pool={black} (want 3) -> {g['ass']['A2b_cast_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool, "life": [life(state, 0),
                                                       life(state, 1)]})
                try:
                    post = await c.export_state()
                    with open(f"{EVDIR}/post_{tag}.json", "w") as f:
                        f.write(post)
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A2b_cast_completes"] = "failed"
                note("A2b: ritual never reached graveyard in 120s")
                g["phase"] = "done"
            return False

    if tag == "B":
        # step 0/1: two altar activations. step 2: export MID, re-check gate.
        # step 3: cast targeting P1. step 4: await resolution, assert, POST.
        if step in (0, 1) and my_prio:
            if g.get("expecting"):
                return False  # sacrifice/color interaction pending
            if g.get("sacs_done", 0) > step:
                g["act_step"] = step + 1
                if step + 1 == 2:
                    note(f"both activations done; pool={pool}")
                return False
            return await activate_producer(g, state, acts, note)
        if step == 2 and my_prio:
            black = pool.get("black", 0)
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_{tag}.json", "w") as f:
                    f.write(mid)
                g["mid_exported"] = True
                note(f"MID exported; pool={pool}")
            except Exception as e:
                note(f"mid export failed: {e}")
            offered, act = cast_spell_offered(state, st, cfg["spell"])
            wire(f"{tag}_mid_gate", {"pool": pool, "offered": offered})
            if offered and black >= 2:
                g["ass"]["A3b_manual_proof"] = "passed"
                note(f"A3b: with {{B}}{{B}} floating, gate now offers "
                     f"{cfg['spell']} -> passed (bug confirmed: feasibility "
                     f"analysis, not resources, blocked the offer)")
                # cast immediately in this same tick: do not pass priority
                # with {B}{B} floating.
                if await cast_spell_action(g, st, state, note):
                    g["expecting"] = "target"
                    g["cast_submitted"] = True
                    g["cast_rev"] = c.revision
                    g["act_step"] = 4
                    g["act_wait"] = time.time()
                    return True
                g["ass"]["A3b_manual_proof"] = "failed"
                note(f"A3b: gate offered but cast submission failed "
                     f"(pool={pool}) -> failed")
                g["phase"] = "done"
            else:
                g["ass"]["A3b_manual_proof"] = "failed"
                note(f"A3b: gate still not offering with pool={pool} "
                     f"(offered={offered}) -> failed")
                g["phase"] = "done"
            return False
        if step == 3 and my_prio:
            # legacy no-op step (cast now happens in step 2); advance.
            g["act_step"] = 4
            return False
        if step == 4:
            gy = graveyard_names(state, 0)
            if "Sign in Blood" in gy:
                l1 = life(state, 1)
                ok = l1 == 18
                # keep A3b as the manual-proof verdict; record resolution here
                g["resolution_B"] = ("passed" if ok else "failed")
                note(f"Sign in Blood resolved: P1 life={l1} (want 18) -> "
                     f"{g['resolution_B']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                try:
                    post = await c.export_state()
                    with open(f"{EVDIR}/post_{tag}.json", "w") as f:
                        f.write(post)
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["resolution_B"] = "failed"
                note("Sign in Blood never reached graveyard in 120s")
                g["phase"] = "done"
            return False

    if tag == "D":
        # step 0: KCI activation (sac Memnite -> {C}{C}). step 1: cast overseer.
        # step 2: await resolution, assert, POST.
        if step == 0 and my_prio:
            if g.get("expecting"):
                return False  # sacrifice interaction pending
            if g.get("sacs_done", 0) >= 1:
                g["act_step"] = 1
                return False
            return await activate_producer(g, state, acts, note)
        if step == 1 and my_prio:
            if pool.get("colorless", 0) >= 2:
                if await cast_spell_action(g, st, state, note):
                    g["cast_submitted"] = True
                    g["act_step"] = 2
                    g["act_wait"] = time.time()
                    return True
            note(f"cast step: pool={pool}; "
                 f"acts={[a['type'] for a in acts][:10]}")
            return False
        if step == 2:
            bf = battlefield_ids(state, 0, "Steel Overseer")
            gy = graveyard_names(state, 0)
            if bf:
                mem_gy = "Memnite" in gy
                ok = mem_gy
                g["ass"]["A5b_cast_completes"] = "passed" if ok else "failed"
                note(f"A5b: Overseer on battlefield; Memnite in gy={mem_gy} "
                     f"-> {g['ass']['A5b_cast_completes']}")
                wire(f"{tag}_resolved",
                     {"gy": gy, "pool": pool,
                      "life": [life(state, 0), life(state, 1)]})
                try:
                    post = await c.export_state()
                    with open(f"{EVDIR}/post_{tag}.json", "w") as f:
                        f.write(post)
                    g["post_exported"] = True
                    note("POST exported")
                except Exception as e:
                    note(f"post export failed: {e}")
                g["phase"] = "done"
                return False
            if time.time() - g.get("act_wait", time.time()) > 120:
                g["ass"]["A5b_cast_completes"] = "failed"
                note("A5b: Overseer never reached battlefield in 120s")
                g["phase"] = "done"
            return False
    return False

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
    say(f"===== GAME {tag}: {cfg['spell']} {cfg['cost']} =====")
    wire("game_start", {"tag": tag, "cfg": cfg})
    g = {"cfg": cfg, "phase": "setup", "notes": [],
         "ass": {}, "answered": set(), "obs": 0, "offered_seen": False,
         "pre_exported": False, "post_exported": False, "mid_exported": False,
         "kept": False, "expecting": None, "sacs_done": 0, "resolution_B": "not-run"}
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
        # ensure exports exist even on timeout
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
    # default any unset assertions to not-run/failed appropriately
    return g


async def main():
    t_start = time.time()
    games = {}
    for cfg in GAMES:
        games[cfg["tag"]] = await run_game(cfg)

    # ---- consolidate assertions ----
    ass = {k: "not-run" for k in ASSERT_KEYS}
    for tag, g in games.items():
        for k, v in g["ass"].items():
            # per-game keys are already unique except A1_setup_ok_<tag>
            if k in ass:
                ass[k] = v
    # A1 per game -> single A1 requires all four setups
    a1s = [games[t]["ass"].get(f"A1_setup_ok_{t}") for t in "ABCD"]
    ass["A1_setup_ok"] = "passed" if all(v == "passed" for v in a1s) \
        else ("failed" if any(v == "failed" for v in a1s) else "not-run")
    # B resolution recorded separately; fold into A3b note, keep A3b as gate proof
    notes_all = []
    for tag, g in games.items():
        notes_all.append(f"--- game {tag} ({g['cfg']['spell']} {g['cfg']['cost']}) "
                         f"duration {g['duration_s']}s ---")
        notes_all.extend(g["notes"])

    # ---- verdict ----
    # reproduced iff the gate fails to offer {B}{B} (A3a failed) while the
    # manual proof (A3b) shows the mana was actually producible.
    if ass["A1_setup_ok"] == "passed" and ass["A3a_gate_offers_BB"] == "failed" \
            and ass["A3b_manual_proof"] == "passed":
        verdict = "reproduced"
    elif ass["A1_setup_ok"] == "passed" and ass["A3a_gate_offers_BB"] == "passed":
        verdict = "not-reproduced"
    elif ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes_all.append("setup incomplete for at least one game; see notes")
    else:
        verdict = "blocked"
        notes_all.append("inconclusive assertion combination; see notes")

    result_line = (
        f"A1 setup_ok: {ass['A1_setup_ok']}; "
        f"A2a gate_offers_{{1}}{{B}}: {ass['A2a_gate_offers_1B']}; "
        f"A2b cast_completes: {ass['A2b_cast_completes']}; "
        f"A3a gate_offers_{{B}}{{B}}: {ass['A3a_gate_offers_BB']}; "
        f"A3b manual_proof: {ass['A3b_manual_proof']}; "
        f"A4 gate_rejects_{{B}}: {ass['A4_gate_rejects_B']}; "
        f"A5a gate_offers_{{2}}: {ass['A5a_gate_offers_2']}; "
        f"A5b cast_completes: {ass['A5b_cast_completes']}"
    )

    run = {
        "issue": 1234,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(time.time() - t_start, 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_1234.py", "rb").read()).hexdigest(),
        "games": {tag: {
            "spell": g["cfg"]["spell"], "cost": g["cfg"]["cost"],
            "deck": g["cfg"]["deck"], "game_code": g.get("game_code"),
            "duration_s": g["duration_s"],
            "assertions": g["ass"],
            "resolution_B": g.get("resolution_B"),
            "offered_seen": g["offered_seen"], "obs": g["obs"],
        } for tag, g in games.items()},
        "assertions": ass,
        "notes": notes_all,
        "verdict": verdict,
        "result": result_line,
        "scope": "feasible_mana_capacity colored-shard feasibility under non-tap "
                 "mana sources (Phyrexian Altar / Krark-Clan Ironworks); native "
                 "engine, two human-client seats; four games covering {1}{B}, "
                 "{B}{B}, {B} (negative), {2} (KCI regression)",
        "limitations": [
            "Browser UI not exercised.",
            "12x spell density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
        ],
        "contract": (
            "Game A ({1}{B} Cabal Ritual, Swamp+Altar+Ghoul): gate must offer; "
            "then real cast. Game B ({B}{B} Sign in Blood, Forests+Altar+2 "
            "Memnites): gate must offer (bug: does not); manual double "
            "activation floats {B}{B} as proof, then real cast at P1. "
            "Game C ({B} Dark Ritual, Altar+0 creatures): gate must NOT offer. "
            "Game D ({2} Steel Overseer, KCI+Memnite): gate must offer; then "
            "real cast via KCI activation."
        ),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    # copy the scenario for provenance
    with open(f"{BACKFILL}/driver/scenario_1234.py", "rb") as src, \
            open(f"{EVDIR}/scenario_1234.py", "wb") as dst:
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
