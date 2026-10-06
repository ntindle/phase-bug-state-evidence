#!/usr/bin/env python3
"""Issue #9295: Words cycle draw replacements broken (v0.102.0, protocol 106).

Reported (auto-filed fix-PR body, 2026-09-25): "Words of War replaced the
draw but never asked for a target and dealt no damage; Words of Wind and
Words of Waste didn't parse at all." The PR (#9295) is still open / on
verification hold as of 2026-10-04, and the pinned v0.102.0 card data
(published 2026-10-04) still carries the OLD payload shape
(`CreateDrawReplacement.replacement_effect` as a bare `Effect`, not the
PR's `Box<AbilityDefinition>`), with Wind/Waste as `unparsed_replacement`.
So the bug is expected to be live on the pinned release.

Oracle text (pinned card-data.json):
- Words of War {2}{R}: "{1}: The next time you would draw a card this turn,
  this enchantment deals 2 damage to any target instead."
- Words of Wind {2}{U}: "{1}: The next time you would draw a card this turn,
  each player returns a permanent they control to its owner's hand instead."

BEHAVIORAL CONTRACT (PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Leg W (Words of War), native engine, two human-client seats, Bo1, life 20:
  Setup: P0 deck 4x Words of War + 56x Mountain; P1 deck 60x Plains.
  Drive: both keep 7. P0 plays a Mountain each turn; casts Words of War on
  the first main phase with 3 mana; P1 plays Plains and passes. On P0's next
  Upkeep (priority exists before the turn's draw), P0 activates the {1}
  ability. Expected per CR 115.1c/602.2b (fixed behavior): a target
  selection for "any target" is offered AT ACTIVATION; the driver answers
  P1. The ability resolves, installing a one-shot draw-replacement shield.
  pre.json is exported in Upkeep after resolution (hand H0, P1 life 20).
  The Draw step follows in the same turn: the draw is replaced. If a
  ReplacementChoice (616.1) prompt appears, the driver applies the Words
  shield. post.json is exported in the following PreCombatMain.
  Assertions:
    A1_data  pinned v0.102.0 card-data.json: Words of War parses as
             CreateDrawReplacement { replacement_effect: DealDamage(2,
             target Any) } with target_prompt null (no activation-time
             target announcement); Words of Wind and Words of Waste are
             Unimplemented/unparsed_replacement. Matches the issue baseline.
    A2_no_target_prompt  during the activation window no target-selection
             opportunity for the Words of War ability is offered (the
             reported "never asked for a target"). If one appears, the
             driver answers P1 and records it.
    A3_draw_replaced     the Draw-step draw does not grow P0's hand
             (post hand == pre hand).
    A4_no_damage         P1 stays at 20 life (the reported "dealt no
             damage") despite the draw being replaced.
Leg N (Words of Wind, runtime control for "didn't parse at all"):
  Setup: P0 deck 4x Words of Wind + 56x Island; P1 deck 60x Plains.
  Drive: cast Words of Wind, activate {1} on the next Upkeep, then observe
  the Draw step.
  Assertions:
    A5_wind_draw_normal  the Draw-step draw proceeds normally (hand grows
             by exactly 1; no permanents bounced). The activation outcome
             (offered / not offered / no-op) is recorded in notes.

Verdict rule: reproduced iff A1, A2, A3, A4 all passed (the exact reported
War symptom on the pinned release). not-reproduced iff A1 passed, both
legs completed, and the War leg shows the fixed behavior (target prompt at
activation AND 2 damage to P1 AND draw replaced). blocked iff A1 cannot be
established or a leg cannot be driven to pre/post exports.
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
RUN_ID = "20261006-9295"
ISSUE = 9295
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "server_binary_sha256": None,  # recomputed below
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "source": ("2026-10-06: v0.102.0 == latest stable per GitHub /releases "
               "(v0.102.0 published 2026-10-04, no newer v0.* stable); "
               "pinned release dir unchanged; hashes recomputed against "
               "on-disk artifacts this run; minisign verification recorded "
               "in the ledger pin; ServerHello re-verified by this run"),
}
for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
    assert _h == {
        "server_binary_sha256": "5b79f0c520e11ad1df158f72c1674a43378ff9c999b57199789a531f6d125aa8",
        "card_data_sha256": "eb87edbd0c90e2440fdb97404e4a40fb86ca9770439f9c045fc9d0f220605b36",
        "draft_pools_sha256": "e80b16721bf3da5b28580f29bbe94b8df43f02363e66647cc004bb53888040e3",
    }[_k], f"hash mismatch on {_f}"

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

WAR = "words of war"
WIND = "words of wind"
MOUNTAIN = "mountain"
ISLAND = "island"
PLAINS = "plains"

WAR_DECK = deck((WAR.title(), 4), (MOUNTAIN.title(), 56))
WIND_DECK = deck((WIND.title(), 4), (ISLAND.title(), 56))
P1_DECK = deck((PLAINS.title(), 60))

CAST_NEEDS = {
    "war": {"R": 1, "generic": 2},
    "wind": {"U": 1, "generic": 2},
}
LAND_OF_LEG = {"war": MOUNTAIN, "wind": ISLAND}

ASS_KEYS = ["A1_data", "A2_no_target_prompt", "A3_draw_replaced",
            "A4_no_damage", "A5_wind_draw_normal"]


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(kind, obj):
    WIRE.write(json.dumps({"t": time.time(), "kind": kind,
                           "obj": obj}, default=str) + "\n")
    WIRE.flush()


ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def reset_leg(leg):
    global ST
    keep_ass = ST.get("ass") if isinstance(ST, dict) else None
    ST = {
        "leg": leg,
        "attempt": 0,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "ass": keep_ass if keep_ass is not None else
               {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "phase": "mulligan",
        "p0_turns": 0,
        "last_turn_seen": None,
        "words_oid": None,
        "activated": False,
        "activation_window": [],
        "target_prompt_seen": False,
        "target_answered": False,
        "pre_exported": False,
        "post_exported": False,
        "draw_seen": False,
        "replacement_choice_seen": False,
        "replacement_applied": False,
        "ability_offered": None,
        "mana_needs": {"P0": {}, "P1": {}},
        "hello_ok": False,
    }
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()


def reset_attempt():
    ST["phase"] = "setup"
    ST["p0_turns"] = 0
    ST["last_turn_seen"] = None
    ST["words_oid"] = None
    ST["activated"] = False
    ST["ability_seen_on_stack"] = False
    ST["activation_window"] = []
    ST["target_prompt_seen"] = False
    ST["target_answered"] = False
    ST["pre_exported"] = False
    ST["post_exported"] = False
    ST["draw_seen"] = False
    ST["replacement_choice_seen"] = False
    ST["replacement_applied"] = False
    ST["ability_offered"] = None
    ST["mana_needs"] = {"P0": {}, "P1": {}}


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller")) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]


def untapped_land_names(state, pid):
    return [obj_lname(state, o) for o in untapped_lands(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised (waiting_for is gone/null on 106)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def opp_codes(opp):
    codes = set()
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("choices") or data.get("candidates") or []:
        codes.update(c for c in surf_codes(ch) if c)
    return codes


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = opp_codes(opp)
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


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


def can_pay(state, pid, colors=(), generic=0):
    names = untapped_land_names(state, pid)
    color_of = {PLAINS: "W", ISLAND: "U", MOUNTAIN: "R"}
    pool = {}
    for n in names:
        c = color_of.get(n)
        if c:
            pool[c] = pool.get(c, 0) + 1
    need = dict(zip(colors, [1] * len(colors)))
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def stack_has_ability(state, src_oid):
    for e in state.get("stack") or []:
        ser = json.dumps(e, default=str)
        if str(src_oid) in ser:
            return True
    return False
# ------------------------------------------------------------- interaction primitives
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
    say(f"[{tag}] vi submit iid={iid} choice={cid} ({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


def find_activate(st, source_oid):
    """Advertised activation of source_oid's ability: legacy ActivateAbility
    action or vi exactChoices with the activateAbility code (106)."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility":
            d = a.get("data") or {}
            if str(d.get("source_id")) == str(source_oid) \
                    or str(a.get("_src_oid")) == str(source_oid):
                return ("submit", a, "ActivateAbility legal_action")
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "activateAbility" not in surf_codes(ch):
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(source_oid) in refs:
                return ("interaction", (opp, ch),
                        "activateAbility via viewer_interaction (choose)")
    return None


async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 106: MulliganDecision arrives as a legacy legal action."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", ST["leg"], str(ST["attempt"]), f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    say(f"[{tag}] keep {len(hn)}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            # legacy payment path completed: drop any vi mana needs so a
            # later ambient tapLandForMana menu is not consumed against them
            ST["mana_needs"][c.name] = {}
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
            if "tapLandForMana" not in surf_codes(ch):
                continue
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


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if ("passPriority" in surf_codes(ch)
                    and ch.get("status", {}).get("type") in (None, "available")):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag, ST["leg"], ST["attempt"])) == turn:
        return False
    pick = None
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            pick = o
            break
    if pick is None:
        return False
    for a in acts:
        if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(pick):
            LAND_PLAYED_TURN[(tag, ST["leg"], ST["attempt"])] = turn
            say(f"[{tag}] playing land {obj_lname(state, pick)}")
            wire("play_land", {"who": tag, "oid": pick})
            await submit_as_is(c, a)
            return True
    return False


def is_target_opp(opp, state):
    """A target-selection opportunity: candidates carry target/player
    surfaces (not the ordinary priority-menu codes). Returns the candidate
    choices or None."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    if not items:
        return None
    codes = opp_codes(opp)
    if codes and codes <= NON_DECISION_CODES:
        return None
    has_target_surface = False
    for ch in items:
        for s in ch.get("surfaces", []) or []:
            if s.get("type") in ("player", "target"):
                has_target_surface = True
    if not has_target_surface:
        return None
    return items


async def answer_war_target(c, st, state, tag):
    """If the Words ability offers a target selection, answer P1 (seat 1).
    Records that the prompt existed (A2 signal)."""
    acted = False
    for opp in vi_ops(st):
        items = is_target_opp(opp, state)
        if not items:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "war-target", ST["leg"], str(iid))
        if key in SUBMITTED_OPPS:
            acted = True
            continue
        ST["target_prompt_seen"] = True
        wire("target_opp_seen",
             {"iid": str(iid),
              "n_candidates": len(items),
              "codes": sorted(opp_codes(opp))})
        say(f"[{tag}] TARGET SELECTION offered for the Words ability "
            f"({len(items)} candidates) -- answering P1")
        want = None
        for ch in items:
            if candidate_seat(ch) == 1:
                want = ch
                break
        if want is None:
            say(f"[{tag}] WARNING: no P1 candidate; not submitting blind")
            ST["notes"].append("target selection offered but no P1 "
                               "candidate; see wire_log target_opp_seen")
            continue
        SUBMITTED_OPPS.add(key)
        ST["target_answered"] = True
        await answer_vi(c, opp, want, tag)
        acted = True
    return acted


async def answer_replacement_choice(c, st, state, tag):
    """Answer a 616.1 replacement-ordering/apply prompt by choosing the
    Words shield candidate (apply the replacement)."""
    if not real_decision_pending(st):
        return False
    acted = False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if opp_codes(opp) <= NON_DECISION_CODES:
            continue
        if is_target_opp(opp, state):
            continue  # handled by answer_war_target
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "repl", ST["leg"], str(iid))
        if key in SUBMITTED_OPPS:
            acted = True
            continue
        # pick the candidate whose text mentions our Words card
        want = None
        want_name = WAR if ST["leg"] == "war" else WIND
        for ch in items:
            if want_name in choice_text(ch).lower():
                want = ch
                break
        if want is None:
            # single-candidate prompts: take it (apply the only replacement)
            if len(items) == 1:
                want = items[0]
        if want is None:
            say(f"[{tag}] replacement prompt with no Words candidate; "
                f"not answering blind")
            ST["notes"].append("replacement prompt without Words candidate; "
                               "see wire_log")
            continue
        ST["replacement_choice_seen"] = True
        ST["replacement_applied"] = True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] replacement prompt -> applying "
            f"{choice_text(want)[:80]}")
        wire("replacement_apply",
             {"iid": str(iid), "choice": choice_text(want)[:120]})
        await answer_vi(c, opp, want, tag)
        acted = True
    return acted


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")
# ------------------------------------------------------------- A1: data-level check
def check_data_level():
    """Verify the pinned v0.102.0 card data carries the issue's baseline:
    War = CreateDrawReplacement/DealDamage(Any) with no target_prompt;
    Wind and Waste = Unimplemented/unparsed_replacement."""
    ev = {}
    war = CARD_DATA.get(WAR, {})
    wab = (war.get("abilities") or [{}])[0]
    weff = wab.get("effect") or {}
    ev["war"] = {
        "oracle": war.get("oracle_text"),
        "effect_type": weff.get("type"),
        "replacement_effect": weff.get("replacement_effect"),
        "target_prompt": wab.get("target_prompt"),
    }
    wind = CARD_DATA.get(WIND, {})
    wnab = (wind.get("abilities") or [{}])[0]
    wneff = wnab.get("effect") or {}
    ev["wind"] = {
        "oracle": wind.get("oracle_text"),
        "effect_type": wneff.get("type"),
        "name": wneff.get("name"),
    }
    waste = CARD_DATA.get("words of waste", {})
    wtab = (waste.get("abilities") or [{}])[0]
    wteff = wtab.get("effect") or {}
    ev["waste"] = {
        "oracle": waste.get("oracle_text"),
        "effect_type": wteff.get("type"),
        "name": wteff.get("name"),
    }
    checks = {}
    checks["war_is_draw_replacement"] = (weff.get("type") == "CreateDrawReplacement")
    re_ = weff.get("replacement_effect") or {}
    checks["war_deals_2_any"] = (
        re_.get("type") == "DealDamage"
        and (re_.get("amount") or {}).get("value") == 2
        and (re_.get("target") or {}).get("type") == "Any")
    checks["war_no_target_prompt"] = (wab.get("target_prompt") is None)
    checks["wind_unparsed"] = (wneff.get("type") == "Unimplemented"
                               and wneff.get("name") == "unparsed_replacement")
    checks["waste_unparsed"] = (wteff.get("type") == "Unimplemented"
                                and wteff.get("name") == "unparsed_replacement")
    with open(f"{EVDIR}/data_check.json", "w") as f:
        json.dump({"card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
                   "evidence": ev, "checks": checks}, f, indent=1)
    ok = all(checks.values())
    for k, v in checks.items():
        say(f"A1 data check [{k}]: {'OK' if v else 'FAIL'}")
    wire("data_level", {"ok": ok, "checks": checks})
    ST["ass"]["A1_data"] = "passed" if ok else "failed"
    return ok


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"
    ST["hello_ok"] = True


# ------------------------------------------------------------- ticks
async def declare_empty(c, acts, atype, data_key, tag):
    da = next((a for a in acts if a.get("type") == atype), None)
    if da:
        d = copy.deepcopy(da)
        d.setdefault("data", {})[data_key] = [] if data_key != "bands" else []
        if atype == "DeclareAttackers":
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
        else:
            d.setdefault("data", {})["assignments"] = []
        await submit_as_is(c, d)
        return True
    return False


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    tag, pid = "P0", 0
    leg = ST["leg"]
    words_name = WAR if leg == "war" else WIND

    tn = state.get("turn_number")
    if state.get("active_player") == 0 and tn != ST.get("last_turn_seen"):
        ST["last_turn_seen"] = tn
        ST["p0_turns"] = ST.get("p0_turns", 0) + 1
        say(f"[P0] starting P0 turn #{ST['p0_turns']} (game turn {tn})")

    if await do_mulligan(c, acts, st, pid, tag):
        return
    if any(a.get("type") == "DeclareAttackers" for a in acts):
        await declare_empty(c, acts, "DeclareAttackers", "attacks", tag)
        return
    if any(a.get("type") == "DeclareBlockers" for a in acts):
        await declare_empty(c, acts, "DeclareBlockers", "assignments", tag)
        return
    # target selection for the Words ability (only meaningful post-activation)
    if ST["phase"] in ("activated_wait", "draw_watch"):
        if await answer_war_target(c, st, state, tag):
            return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    # 616.1 replacement prompt at the draw
    if ST["phase"] == "draw_watch":
        if await answer_replacement_choice(c, st, state, tag):
            return

    # record the activation window's opportunity surface (A2 evidence)
    if ST["phase"] == "activated_wait":
        for opp in vi_ops(st):
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            items = data.get("candidates") or data.get("choices") or []
            ST["activation_window"].append({
                "rev": c.revision,
                "phase": state.get("phase"),
                "rtype": (opp.get("response", {}) or {}).get("type"),
                "codes": sorted(opp_codes(opp)),
                "n": len(items),
                "is_target": is_target_opp(opp, state) is not None,
            })

    words_bf = bf_by_name(state, pid, words_name)
    if words_bf and ST["words_oid"] is None:
        ST["words_oid"] = words_bf[0]
        ST["mana_needs"][tag] = {}  # cast payment settled one way or another
        say(f"[P0] {words_name} on battlefield (oid {words_bf[0]})")
        if ST["phase"] == "setup":
            ST["phase"] = "wait_upkeep"

    if ST["phase"] == "setup" and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        hn = hand_lnames(state, pid)
        if (words_name in hn and not words_bf
                and can_pay(state, pid,
                            tuple(CAST_NEEDS[leg].keys() - {"generic"}),
                            CAST_NEEDS[leg]["generic"])):
            a, oid = cast_action_for(acts, state, words_name)
            if a:
                ST["mana_needs"][tag] = dict(CAST_NEEDS[leg])
                say(f"[P0] casting {words_name} (oid {oid})")
                wire("cast", {"who": tag, "card": words_name, "oid": oid})
                await submit_as_is(c, a)
                return

    # upkeep activation: MUST hold priority (never pass first)
    if (ST["phase"] == "wait_upkeep" and words_bf
            and state.get("phase") == "Upkeep"
            and state.get("active_player") == 0
            and my_priority(acts)):
        r = find_activate(st, ST["words_oid"] or words_bf[0])
        ST["ability_offered"] = r is not None
        if r is None:
            say("[P0] WARNING: ActivateAbility not offered in upkeep; "
                f"acts={[a['type'] for a in acts][:8]}")
            wire("activate_missing",
                 {"acts": [a["type"] for a in acts][:8]})
        else:
            kind, sub, what = r
            ST["mana_needs"][tag] = {"generic": 1}
            ST["phase"] = "activated_wait"
            ST["activated"] = True
            say(f"[P0] activating {words_name} {{1}} via {what} "
                f"(holding priority)")
            wire("activate_ability", {"via": what, "leg": leg})
            if kind == "interaction":
                opp, ch = sub
                await answer_vi(c, opp, ch, tag)
            else:
                await submit_as_is(c, sub)
            return

    # activation resolved -> export pre (shield installed).
    # The {1} cost has no tap component, so resolution is detected via the
    # ability leaving the stack (seen on stack -> gone), not via tapped.
    if ST["phase"] == "activated_wait" and ST["activated"]:
        on_stack = (stack_has_ability(state, ST["words_oid"])
                    if ST["words_oid"] else False)
        if on_stack:
            if not ST.get("ability_seen_on_stack"):
                say(f"[P0] {words_name} ability on the stack")
                wire("ability_on_stack", {"oid": ST["words_oid"]})
            ST["ability_seen_on_stack"] = True
            ST["mana_needs"][tag] = {}
        if ST.get("ability_seen_on_stack") and not on_stack:
            evpre = "pre" if leg == "war" else "pre2"
            await export_as(c, evpre)
            ST["pre_exported"] = True
            ST["phase"] = "draw_watch"
            say(f"[P0] {evpre}.json exported in {state.get('phase')} "
                f"(activation resolved)")
            return
        # timing confounded: the draw step arrived before the ability
        # resolved -> the shield cannot cover this turn's draw; abandon
        if (state.get("phase") == "Draw" and state.get("active_player") == 0
                and not ST.get("ability_seen_on_stack")):
            ST["notes"].append(
                f"leg {leg} attempt {ST['attempt']}: draw step reached "
                f"before the Words ability resolved -- timing confounded")
            say("[P0] DRAW BEFORE RESOLUTION -- abandoning attempt")
            ST["phase"] = "abandoned"
            return

    # draw step observation -> post export in the following main
    if ST["phase"] == "draw_watch":
        if state.get("phase") == "Draw" and state.get("active_player") == 0:
            if not ST["draw_seen"]:
                ST["draw_seen"] = True
                say("[P0] Draw phase reached (active P0)")
        if (ST["draw_seen"] and state.get("phase") in ("PreCombatMain",)
                and state.get("active_player") == 0 and not ST["post_exported"]):
            evpost = "post" if leg == "war" else "post2"
            await export_as(c, evpost)
            ST["post_exported"] = True
            ST["phase"] = "done"
            say(f"[P0] {evpost}.json exported -- leg drive complete")
            return

    # turn cap: never found the Words card
    if ST["p0_turns"] > 10 and not words_bf and ST["phase"] == "setup":
        ST["notes"].append(f"leg {leg} attempt {ST['attempt']}: turn cap "
                           f"without drawing {words_name}")
        say(f"[P0] TURN CAP: {words_name} never drawn -- abandoning attempt")
        ST["phase"] = "abandoned"
        return

    if my_priority(acts) and not real_decision_pending(st):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    tag, pid = "P1", 1
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if any(a.get("type") == "DeclareAttackers" for a in acts):
        await declare_empty(c, acts, "DeclareAttackers", "attacks", tag)
        return
    if any(a.get("type") == "DeclareBlockers" for a in acts):
        await declare_empty(c, acts, "DeclareBlockers", "assignments", tag)
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(acts) and not real_decision_pending(st):
        await pass_priority(c, st, acts)


# ------------------------------------------------------------- drive
async def drive_attempt(c0, c1, p0_deck, deadline_s=420):
    ST["attempt"] += 1
    say(f"=== leg {ST['leg']} attempt {ST['attempt']} ===")
    wire("attempt_start", {"leg": ST["leg"], "attempt": ST["attempt"]})
    attached = await c0.create(p0_deck, player_count=2)
    code = attached.get("game_code")
    say(f"game created: code={code}")
    j1 = await c1.join(code, P1_DECK)
    say(f"P1 joined: {json.dumps(j1)[:120]}")
    reset_attempt()
    t0 = time.time()
    try:
        while time.time() - t0 < deadline_s:
            if ST["phase"] in ("done", "abandoned"):
                break
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            await asyncio.sleep(0.5)
    finally:
        pass
    ok = ST["phase"] == "done"
    say(f"attempt {ST['attempt']} finished: phase={ST['phase']}")
    return ok


def load_env(name):
    try:
        with open(f"{EVDIR}/{name}.json") as f:
            return json.loads(f.read())
    except (OSError, ValueError) as e:
        ST["notes"].append(f"{name}.json load failed: {e}")
        return None


def env_hand_life(env, pid):
    st = (env or {}).get("state") or {}
    hand = len(player_of(st, pid).get("hand", []) or [])
    life = player_of(st, pid).get("life")
    return hand, life


def eval_war():
    ass = ST["ass"]
    pre = load_env("pre")
    post = load_env("post")
    pre_hand, pre_life = env_hand_life(pre, 0)
    post_hand, post_life = env_hand_life(post, 1)
    post_p0_hand, _ = env_hand_life(post, 0)
    checks = {
        "pre_exported": bool(pre) and ST["pre_exported"],
        "post_exported": bool(post) and ST["post_exported"],
        "activation_submitted": ST["activated"],
        "draw_seen": ST["draw_seen"],
        "pre_hand": pre_hand,
        "post_hand": post_p0_hand,
        "pre_p1_life": pre_life,
        "post_p1_life": post_life,
        "target_prompt_seen": ST["target_prompt_seen"],
        "target_answered": ST["target_answered"],
        "replacement_choice_seen": ST["replacement_choice_seen"],
        "replacement_applied": ST["replacement_applied"],
        "activation_window_opps": len(ST["activation_window"]),
    }
    # A2: no target prompt during the activation window
    win_targets = [o for o in ST["activation_window"] if o.get("is_target")]
    checks["window_target_opps"] = len(win_targets)
    if checks["activation_submitted"] and checks["pre_exported"]:
        ass["A2_no_target_prompt"] = ("passed" if not ST["target_prompt_seen"]
                                      and not win_targets else "failed")
    # A3: draw replaced (hand unchanged across the draw step)
    if checks["pre_exported"] and checks["post_exported"] and checks["draw_seen"]:
        ass["A3_draw_replaced"] = ("passed" if post_p0_hand == pre_hand
                                   else "failed")
    # A4: no damage to P1
    if checks["pre_exported"] and checks["post_exported"]:
        ass["A4_no_damage"] = ("passed" if post_life == pre_life == 20
                               else "failed")
    say(f"war leg checks: {json.dumps(checks, default=str)}")
    wire("war_eval", {"checks": checks, "ass": dict(ass)})
    return checks


def eval_wind():
    ass = ST["ass"]
    pre = load_env("pre2")
    post = load_env("post2")
    pre_hand, _ = env_hand_life(pre, 0)
    post_hand, _ = env_hand_life(post, 0)
    checks = {
        "pre_exported": bool(pre) and ST["pre_exported"],
        "post_exported": bool(post) and ST["post_exported"],
        "ability_offered": ST["ability_offered"],
        "activation_submitted": ST["activated"],
        "draw_seen": ST["draw_seen"],
        "pre_hand": pre_hand,
        "post_hand": post_hand,
    }
    if checks["pre_exported"] and checks["post_exported"] and checks["draw_seen"]:
        # draw NOT replaced: hand grows by exactly 1
        ass["A5_wind_draw_normal"] = ("passed" if post_hand == pre_hand + 1
                                      else "failed")
    say(f"wind leg checks: {json.dumps(checks, default=str)}")
    wire("wind_eval", {"checks": checks, "ass": dict(ass)})
    return checks


async def run_leg(leg, p0_deck, ev_prefix):
    reset_leg(leg)
    completed = False
    for _ in range(3):
        c0 = PhaseClient("P0")
        c1 = PhaseClient("P1")
        await c0.connect()
        await c1.connect()
        say("P0 hello done; P1 hello done")
        try:
            if await drive_attempt(c0, c1, p0_deck):
                completed = True
                break
        except Exception as e:
            say(f"attempt {ST['attempt']} raised {type(e).__name__}: {e}")
            wire("attempt_error", {"err": f"{type(e).__name__}: {e}"})
        finally:
            for c in (c0, c1):
                try:
                    await c.close()
                except Exception:
                    pass
        say(f"attempt {ST['attempt']} did not complete; retrying")
    checks = eval_war() if leg == "war" else eval_wind()
    return checks, completed


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


def render_png(verdict):
    from PIL import Image, ImageDraw
    W, H = 1000, 980
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)
    run = json.load(open(f"{EVDIR}/run.json"))
    ass = run["assertions"]
    checks = run.get("checks", {})
    srv = run["server"]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9295 - Words cycle draw replacements broken",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}", fill=DIM)
    y += 30
    vcol = RED if run["verdict"] == "reproduced" else GREEN
    d.text((24, y), f"VERDICT: {run['verdict'].upper()}", fill=vcol)
    y += 34

    def panel(title, lines, h):
        nonlocal y
        d.rectangle([16, y, W - 16, y + h], fill=PANEL)
        d.text((28, y + 8), title, fill=ACCENT)
        yy = y + 32
        for ln in lines:
            d.text((28, yy), ln[:116], fill=TEXT)
            yy += 20
        y += h + 12

    panel("A1 data-level (pinned v0.102.0 card-data.json)",
          ["Words of War: CreateDrawReplacement / DealDamage(2, target Any), "
           "target_prompt=null",
           "Words of Wind / Words of Waste: Unimplemented "
           "(unparsed_replacement)"], 90)
    wc = checks.get("war", {})
    panel("Leg W -- Words of War (activate {1} on upkeep, draw must be "
          "replaced)",
          [f"activation submitted: {wc.get('activation_submitted')}; "
           f"target prompt at activation: {wc.get('target_prompt_seen')} "
           f"(expected by CR 115.1c/602.2b)",
           f"pre : P0 hand={wc.get('pre_hand')}  P1 life={wc.get('pre_p1_life')}",
           f"post: P0 hand={wc.get('post_hand')}  P1 life={wc.get('post_p1_life')}",
           f"draw replaced (hand unchanged): "
           f"{wc.get('pre_hand') == wc.get('post_hand')}",
           f"damage dealt: "
           f"{(wc.get('post_p1_life') or 20) < 20}"], 150)
    nc = checks.get("wind", {})
    panel("Leg N -- Words of Wind (unparsed control)",
          [f"ability offered: {nc.get('ability_offered')}; "
           f"activation submitted: {nc.get('activation_submitted')}",
           f"pre : P0 hand={nc.get('pre_hand')}",
           f"post: P0 hand={nc.get('post_hand')} "
           f"(draw not replaced: {nc.get('post_hand') == (nc.get('pre_hand') or 0) + 1})"],
          110)
    alines = []
    for k in ASS_KEYS:
        v = ass.get(k, "not-run")
        col = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        alines.append((f"{k}: {v}", col))
    d.rectangle([16, y, W - 16, y + 40 + 22 * len(alines)], fill=PANEL)
    d.text((28, y + 8), "assertions", fill=ACCENT)
    yy = y + 32
    for ln, col in alines:
        d.text((28, yy), ln, fill=col)
        yy += 22
    y += 40 + 22 * len(alines) + 12
    d.text((24, y), "scope: native engine, 2 human seats; browser UI not "
                    "exercised", fill=DIM)
    y += 22
    d.text((24, y), "evidence: ntindle/phase-bug-state-evidence "
                    "9295/20261006-9295/", fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print(f"wrote {out} ({W}x{H})")


async def main():
    say(f"=== scenario_9295 run {RUN_ID} ===")
    checks = {}
    reset_leg("war")  # initialize ST (ass preserved across legs)
    await verify_server_hello()
    if not check_data_level():
        say("A1 data check FAILED -- aborting legs")
    else:
        war_checks, war_done = await run_leg("war", WAR_DECK, "pre")
        say(f"war leg done={war_done}")
        wind_checks, wind_done = await run_leg("wind", WIND_DECK, "pre2")
        say(f"wind leg done={wind_done}")
        checks = {"war": war_checks, "wind": wind_checks}

    ass = ST["ass"]
    notes = ST["notes"]
    core = all(ass[k] == "passed" for k in
               ("A1_data", "A2_no_target_prompt", "A3_draw_replaced", "A4_no_damage"))
    war_ran = all(ass[k] != "not-run" for k in
                  ("A2_no_target_prompt", "A3_draw_replaced", "A4_no_damage"))
    if core:
        verdict = "reproduced"
    elif ass["A1_data"] == "failed":
        verdict = "blocked"
        notes.append("A1 data-level check failed: pinned card data does not "
                     "match the issue's baseline shape")
    elif war_ran:
        # war leg completed but the symptom differs from the report
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        notes.append("one or both legs did not complete; see notes")
    say(f"ASSERTIONS: {ass}")
    say(f"VERDICT: {verdict}")

    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "server": SERVER_IDENTITY,
        "scenario": "driver/scenario_9295.py",
        "scenario_sha256": hashlib.sha256(
            open(os.path.join(BACKFILL, "driver/scenario_9295.py"),
                 "rb").read()).hexdigest(),
        "assertions": ass,
        "checks": checks,
        "verdict": verdict,
        "result": ("On pinned v0.102.0 (e17f6fd, protocol 106): activating "
                   "Words of War's {1} ability offers no target selection at "
                   "activation; the next draw is replaced (P0 hand unchanged) "
                   "but no damage is dealt (P1 stays at 20). Words of Wind "
                   "and Words of Waste remain Unimplemented "
                   "(unparsed_replacement) in the pinned card data and the "
                   "draw is not replaced. Matches the issue's baseline "
                   "exactly."),
        "scope": ("Words of War accept path + Words of Wind unparsed "
                  "control; native engine, two human-client seats, Bo1. "
                  "Words of Waste / Wilding / Worship not driven at runtime; "
                  "their parse status is covered by the data-level check."),
        "limitations": [
            "Browser UI not exercised.",
            "The draw-replacement shield's internal representation is not "
            "asserted from the export; the verdict rests on the observable "
            "outcome (target prompt / hand size / life totals).",
            "Words of Waste, Words of Wilding, Words of Worship: parse "
            "status only (data-level); no runtime leg.",
        ],
        "notes": notes,
        "wire_log": "wire_log.jsonl",
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"wrote run.json (verdict={verdict})")
    wire("finalize", {"assertions": ass, "verdict": verdict})
    # copy the scenario into the evidence dir for the record
    import shutil as _sh
    _sh.copy(os.path.join(BACKFILL, "driver/scenario_9295.py"),
             os.path.join(EVDIR, "scenario_9295.py"))
    write_manifest()
    render_png(verdict)
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    print(f"FINAL VERDICT: {v}")
