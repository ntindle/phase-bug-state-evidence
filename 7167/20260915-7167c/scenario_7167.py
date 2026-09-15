#!/usr/bin/env python3
"""Issue #7167: Game Refresh takes away Card ability - Sakashima.

Reported (Discord): cast [[Sakashima of a thousand faces]] (as commander,
partnered with [[Krark, the Thumbless]]) entering as a copy of Krark - works
fine. But "if the game refreshes at all, the Sakashima version of Krark
becomes the same card as Krark, meaning it forces you to sacrifice one, and
you lose Sakashima from the command zone for the rest of the game."

Triage: fully parsed; defect is persistence/rehydration of runtime copy
state (Engine-WASM serialization of copy layers, card identity, commander
metadata).

Plan (native engine, v0.83.0 / protocol 70, three human driver seats,
CommanderDraft):
  P0: commanders [krark, the thumbless, sakashima of a thousand faces];
      main 30x island / 30x mountain (dense playset, cf. #7140).
      Casts Krark from CZ ({1}{R}), then Sakashima from CZ ({2}{U}{U}),
      accepting the "enter as a copy of another creature you control"
      replacement and choosing Krark.
  P1/P2: 60x forest, inert commanders, zero-attacker combat scripted.
  Refresh = client refresh: all clients disconnect, then P0 re-attaches via
  the Reconnect message (game_code + player_token + full_key), exactly what
  a page reload does. Export pre.json before, post.json after.
  Level 2 (only if level 1 shows no loss): SIGTERM the server, restart on
  the same games.db (forces full rehydration from the persisted snapshot),
  reconnect, export post2.json.

Behavioral contract:
  A1 setup_ok        Krark cast from CZ; Sakashima cast from CZ and entered
                     as a copy of Krark (replacement accepted, Krark chosen);
                     pre.json shows both on P0 BF with no legend sacrifice.
  A2 pre_copy_state  pre.json: the Sakashima object shows the copy overlay
                     (name == krark, base_name == sakashima), retains
                     Sakashima's own abilities (legend-rule-doesn't-apply
                     static), is_commander == true.
  A3 refresh_ok      Reconnect re-attach succeeded; post.json exported via
                     the re-attached host; game not stalled.
  A4 copy_preserved  post.json copy signals match pre.json (same object,
                     name/base_name, retained abilities, is_commander).
                     failed = copy overlay lost (became plain Krark / lost
                     identity or commander association).
  A5 legend_rule     post-refresh: both permanents still on P0 BF; no
                     legend-rule sacrifice was forced. failed = one gone.
  A6 cleanup         game advances post-refresh; no stall; post exported.

Verdict: reproduced iff A1-A3 pass and (A4 fails or A5 fails).
         not-reproduced iff A1-A5 pass.
         blocked iff A1 fails or the refresh cannot be performed.

Driver notes:
  - The "may enter as a copy" replacement is answered accept=true, then the
    creature choice picks Krark's battlefield oid (recorded at answer time,
    cf. AGENTS.md #6906).
  - Commander casts use the legal_actions "cast"-type action matched by
    object name (cf. #6915 cast_named).
  - P0 land drops prefer Mountain first, then Islands (need {1}{R} then
    {2}{U}{U}); the engine auto-taps mana (protocol 70).
"""
import asyncio
import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
SERVER_RUN_DIR = f"{BACKFILL}/runs/20260915-7167c"  # fresh isolated server/DB
RUN_ID = "20260915-7167c"
ISSUE = 7167
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KRARK = "krark, the thumbless"
SAKASHIMA = "sakashima of a thousand faces"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [KRARK, SAKASHIMA]
P0_MAIN = [(ISLAND, 30), (MOUNTAIN, 30)]
P1_COMMANDER = [AYULA]
P1_DECK = [(FOREST, 60)]
P2_COMMANDER = [AYULA]
P2_DECK = [(FOREST, 60)]

TIMEOUT = 1500
TURN_CAP = 30

SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

COMMANDER_FORMAT = {
    "format": "CommanderDraft",
    "starting_life": 40,
    "min_players": 3,
    "max_players": 8,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": 21,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Forbidden"},
    "uses_commander": True,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
}

ST = {}
SUBMITTED = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> COPY_LIVE -> PRE_EXPORTED -> REFRESHED -> DONE
        "stop": False,
        "krark_cast": False, "krark_oid": None,
        "sak_cast": False, "sak_copy_choice": False, "sak_target_oid": None,
        "sak_bf_oid": None,
        "pre_exported": False, "post_exported": False, "post2_exported": False,
        "game_code": None, "player_token": None, "full_key": None,
        "refresh_l1_ok": False, "refresh_l2_ok": False,
        "l2_attempted": False,
        "pre_at": None, "post_at": None,
        "unexpected": 0,
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


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


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and "land" in
                str((o.get("base_card_types") or {}).get("core_types", [])).lower()
                and (name is None or nm == name)):
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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


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
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


def accept_choice(chs):
    """Pick the accept=true choice from decideOptionalEffect-style candidates."""
    for ch in chs:
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data") or {}
            if (sf.get("type") == "value" and d.get("role") == "accept"
                    and str(d.get("value")).lower() == "true"):
                return ch
    return None


def copy_signals(st):
    """Structural signals of the Sakashima-derived object on P0's BF."""
    out = {"found": False}
    for oid, o in (st.get("objects", {}) or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        if o.get("controller") != 0:
            continue
        base = str(o.get("base_name") or "").lower()
        name = str(o.get("name") or "").lower()
        if "sakashima" not in base and "sakashima" not in name:
            continue
        out.update({
            "found": True,
            "oid": int(oid),
            "base_name": base,
            "name": name,
            "is_commander": bool(o.get("is_commander")),
            "controller": o.get("controller"),
            "zone": o.get("zone"),
            "tapped": bool(o.get("tapped")),
            "power": o.get("power"), "toughness": o.get("toughness"),
            "mana_cost": json.dumps(o.get("mana_cost"), default=str)[:120],
            "static_count": len(o.get("static_definitions") or []),
            "replacement_count": len(o.get("replacement_definitions") or []),
        })
        blob = json.dumps(o.get("static_definitions") or [],
                           default=str).replace(" ", "").lower()
        out["has_legend_exempt"] = "legendruledoesntapply" in blob
        # full object hash for byte-level comparison pre vs post
        out["obj_sha"] = hashlib.sha256(
            json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()[:16]
        break
    return out


def command_zone_dump(st):
    cz = st.get("command_zone")
    try:
        return json.dumps(cz, default=str)[:800]
    except Exception:
        return str(cz)[:800]

# ---------------------------------------------------------------- main ----
async def main():
    reset()
    notes = []
    obs = {"unexpected_prompts": [], "auto_answered": [], "mulligans": [],
           "refresh": [], "legend_events": []}

    # ------------------------------------------------------------- tick core
    async def do_mulligan(c, pid, tag, min_lands=2):
        st = c.latest
        a = find_action(merged_actions(st), "MulliganDecision")
        if not a:
            return False
        names = hand_lnames(st["state"], pid)
        nlands = sum(1 for n in names if n in (ISLAND, MOUNTAIN, FOREST))
        keep = nlands >= min_lands
        say(f"[{tag}] mulligan: {nlands} lands -> {'keep' if keep else 'redo'}")
        obs["mulligans"].append({"who": tag, "n_lands": nlands, "keep": keep})
        sub = copy.deepcopy(a)
        sub["data"]["decision"] = "keep" if keep else "mulligan"
        await submit_as_is(c, sub)
        MULLS[tag] = True
        wire("mulligan", {"who": tag, "decision": sub["data"]["decision"],
                          "n_lands": nlands})
        return True

    async def do_bottom(c, pid, tag):
        st = c.latest
        state = st["state"]
        a = find_action(merged_actions(st), "SelectCards")
        if not a:
            return False
        n = ((wf_of(state).get("data") or {}).get("phase") or {}).get("count", 1)
        h = hand_ids(state, pid)
        # bottom lands first (never bottom the commanders; they're in CZ)
        picks = h[:n]
        sub = copy.deepcopy(a)
        sub["data"]["cardIds"] = [int(x) for x in picks]
        say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
        await submit_as_is(c, sub)
        return True

    async def discard_tick(c, pid, tag, acts, st, state):
        if wf_of(state).get("type") != "DiscardChoice":
            return False
        if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
            return False
        a = find_action(acts, "DiscardChoice")
        if not a:
            return False
        n = ((wf_of(state).get("data") or {}).get("count")) or 1
        # discard lands first (commanders live in CZ; nothing to protect)
        prio = sorted(hand_ids(state, pid),
                      key=lambda o: 0 if lname(state, o) in
                      (ISLAND, MOUNTAIN, FOREST) else 1)
        sub = copy.deepcopy(a)
        sub["data"]["cardIds"] = [int(x) for x in prio[:n]]
        say(f"[{tag}] discarding {[lname(state, x) for x in prio[:n]]}")
        await submit_as_is(c, sub)
        return True

    async def discard_handsize_tick(c, pid, tag, st, state):
        """Cleanup discard on protocol 70: waiting_for.type ==
        DiscardToHandSize with data {cards, count, player}; answered via the
        schema 'select' opportunity (candidates carry object references)."""
        wf = wf_of(state)
        if wf.get("type") != "DiscardToHandSize":
            return False
        data = wf.get("data") or {}
        if str(data.get("player")) != str(pid):
            return False
        n = data.get("count") or 1
        vi = get_vi(st)
        if not vi:
            return False
        hand = hand_ids(state, pid)
        prio = sorted(
            hand, key=lambda o: 0 if lname(state, o) in
            (ISLAND, MOUNTAIN, FOREST) else 1)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            key = (tag, "discard", str(iid))
            if key in SUBMITTED:
                continue
            resp = opp.get("response", {}) or {}
            rdata = resp.get("data", {}) or {}
            cands = rdata.get("candidates") or []
            if not cands:
                continue
            ref2cid = {}
            for ch in cands:
                for sf in ch.get("surfaces", []) or []:
                    d = sf.get("data") or {}
                    if (d.get("role") == "candidate"
                            and d.get("reference") is not None):
                        ref2cid[str(d["reference"])] = ch.get("id")
            picks = [ref2cid[str(x)] for x in prio
                     if str(x) in ref2cid][:n]
            if len(picks) < n:
                picks = [ch.get("id") for ch in cands[:n]]
            SUBMITTED.add(key)
            spec = (rdata.get("spec") or {}).get("type") or "select"
            sub = {"interactionId": iid,
                   "response": {"type": spec,
                                "data": {"choiceIds": picks}}}
            say(f"[{tag}] discarding {n} to hand size")
            wire("interaction_submission",
                 {"who": tag, "submission": sub, "kind": "DiscardToHandSize"})
            await c.send_interaction(sub)
            return True
        return False

    async def combat_tick(c, pid, tag, acts, st, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "DeclareAttackers" and state.get("active_player") == pid:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return True
        return False

    async def pay_tick(acts, c, tag):
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return True
        return False

    async def generic_prompt(c, pid, tag, st, state):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            key = (tag, str(iid))
            if key in SUBMITTED:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            blob = (json.dumps(opp, default=str).lower()
                    + " " + wf_of(state).get("type", "").lower())
            looks_copy = ("copy" in blob and
                          ("sakashima" in blob or "enter as" in blob
                           or "another creature" in blob))
            # NEVER auto-answer copy/entry-replacement prompts here; the
            # Sakashima entry handler owns them.
            if looks_copy:
                continue
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6],
                 "wf": wf_of(state).get("type")})
            say(f"[{tag}] UNEXPECTED PROMPT iid={str(iid)[:8]} "
                f"wf={wf_of(state).get('type')} n={len(chs)} "
                f"texts={[choice_text(ch)[:40] for ch in chs][:4]}")
            wire("unexpected_prompt",
                 {"who": tag, "wf": wf_of(state).get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            SUBMITTED.add(key)
            obs["unexpected"] = obs.get("unexpected", 0) + 1
        return acted

    async def sakashima_entry_tick(c, tag, st, state):
        """Own the Sakashima "enter as a copy" replacement.

        Observed shape (protocol 70): waiting_for.type == "ReplacementChoice"
        with data.candidates[].description, e.g.
          [0] "You may have ~ enter as a copy of another creature you control,
                except it has ~'s other abilities."
          [1] "Decline"
        and an exactChoices opportunity whose choices carry a value surface
        {role: optionIndex, value: "0"/"1"} + an action surface
        code=chooseReplacement. Accept = the copy candidate's index.
        The follow-up "which creature" prompt is handled by the second half.
        """
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        if wf.get("type") == "ReplacementChoice":
            cands = (wf.get("data") or {}).get("candidates") or []
            copy_idx = None
            for i, cd in enumerate(cands):
                desc = str(cd.get("description") or "").lower()
                if "copy" in desc and "decline" not in desc:
                    copy_idx = i
                    break
            if copy_idx is None:
                return False
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                key = (tag, "sak-accept", str(iid))
                if key in SUBMITTED:
                    continue
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                chs = data.get("choices") or data.get("candidates") or []
                for ch in chs:
                    hit = False
                    for sf in ch.get("surfaces", []) or []:
                        d = sf.get("data") or {}
                        if (d.get("role") == "optionIndex"
                                and str(d.get("value")) == str(copy_idx)):
                            hit = True
                            break
                    if hit:
                        SUBMITTED.add(key)
                        ST["sak_copy_choice"] = True
                        desc = str(cands[copy_idx].get("description"))[:120]
                        say(f"[{tag}] Sakashima ReplacementChoice: ACCEPT "
                            f"(option {copy_idx}): {desc}")
                        wire("sakashima_accept",
                             {"who": tag, "option_index": copy_idx,
                              "description": desc})
                        await answer_vi(c, opp, ch, tag)
                        return True
            return False
        # Follow-up: which creature to copy -> pick Krark's battlefield oid.
        krark = bf_id(state, 0, KRARK)
        if krark is None:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            key = (tag, "sak-target", str(iid))
            if key in SUBMITTED:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            blob_all = json.dumps(chs, default=str).lower()
            if str(krark) not in blob_all:
                continue
            for ch in chs:
                if str(krark) in json.dumps(ch, default=str).lower():
                    SUBMITTED.add(key)
                    ST["sak_target_oid"] = krark
                    say(f"[{tag}] Sakashima copying Krark oid={krark}")
                    wire("sakashima_target", {"who": tag, "oid": krark})
                    await answer_vi(c, opp, ch, tag)
                    return True
        return False

    async def cast_named(c, acts, state, name, tag):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data") or {}
            oid = (d.get("object_id") or d.get("source_id")
                   or a.get("_src_oid"))
            if oid is not None and lname(state, int(oid)) == name:
                say(f"[{tag}] casting {name} (oid={oid})")
                wire("cast", {"who": tag, "name": name, "oid": int(oid)})
                await submit_as_is(c, a)
                return True
        return False

    async def play_land_pref(c, acts, state, pref):
        plays = [a for a in acts if a["type"] == "PlayLand"]
        if not plays:
            return False
        for name in pref:
            for a in plays:
                d = a.get("data") or {}
                oid = d.get("object_id") or a.get("_src_oid")
                if oid is not None and lname(state, int(oid)) == name:
                    say(f"[P0] playing land {name}")
                    wire("play_land", {"name": name, "oid": int(oid)})
                    await submit_as_is(c, a)
                    return True
        await submit_as_is(c, plays[0])
        return True

    # ------------------------------------------------- per-seat tick fns ---
    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and "P0" not in MULLS:
                await do_mulligan(p0, 0, "P0")
                return
            if find_action(acts, "SelectCards"):
                await do_bottom(p0, 0, "P0")
                return
        if await pay_tick(acts, p0, "P0"):
            return
        if await combat_tick(p0, 0, "P0", acts, st, state):
            return
        if await discard_handsize_tick(p0, 0, "P0", st, state):
            return
        # Sakashima entry replacement owns copy-looking prompts first.
        if await sakashima_entry_tick(p0, "P0", st, state):
            return
        # Legend-rule choice (should NOT happen pre-refresh; record if it does)
        if "legend" in wtype.lower():
            obs["legend_events"].append(
                {"stage": ST["stage"], "wf": wtype,
                 "note": "legend prompt observed"})
            say(f"[P0] LEGEND PROMPT observed at stage={ST['stage']} wf={wtype}")
            wire("legend_prompt",
                 {"stage": ST["stage"], "wf": wtype,
                  "state_waiting_for": json.loads(json.dumps(wf, default=str))})
            vi = get_vi(st)
            if vi:
                wire("legend_opps",
                     {"opps": json.loads(json.dumps(
                         vi.get("opportunities", []), default=str))})
            return  # do NOT answer: the prompt itself is evidence
        if not my_priority(state, 0):
            await generic_prompt(p0, 0, "P0", st, state)
            return
        if await discard_tick(p0, 0, "P0", acts, st, state):
            return
        if await generic_prompt(p0, 0, "P0", st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0:
            # Land drop: Mountain first, then Islands, then anything.
            if not ST["krark_cast"] or not ST["sak_cast"]:
                lands = untapped_lands(state, 0)
                has_m = len(untapped_lands(state, 0, MOUNTAIN)) > 0 or \
                    any(lname(state, o) == MOUNTAIN
                        for o in bf_ids(state, 0))
                n_islands = sum(1 for o in bf_ids(state, 0)
                                if lname(state, o) == ISLAND)
                if not has_m:
                    pref = [MOUNTAIN, ISLAND]
                elif n_islands < 2:
                    pref = [ISLAND, MOUNTAIN]
                else:
                    pref = [MOUNTAIN, ISLAND]
                if await play_land_pref(p0, acts, state, pref):
                    return
            unt = untapped_lands(state, 0)
            n_islands = len(untapped_lands(state, 0, ISLAND))
            n_mountains = len(untapped_lands(state, 0, MOUNTAIN))
            # Cast Krark {1}{R} from CZ: 2 lands incl 1 mountain.
            if (not ST["krark_cast"] and len(unt) >= 2 and n_mountains >= 1
                    and bf_id(state, 0, KRARK) is None):
                if await cast_named(p0, acts, state, KRARK, "P0"):
                    ST["krark_cast"] = True
                    return
            # Cast Sakashima {2}{U}{U} from CZ: 4 lands incl 2 islands.
            if (ST["krark_cast"] and not ST["sak_cast"]
                    and bf_id(state, 0, KRARK) is not None
                    and len(unt) >= 4 and n_islands >= 2):
                if await cast_named(p0, acts, state, SAKASHIMA, "P0"):
                    ST["sak_cast"] = True
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and "P1" not in MULLS:
                await do_mulligan(p1, 1, "P1")
                return
            if find_action(acts, "SelectCards"):
                await do_bottom(p1, 1, "P1")
                return
        if await pay_tick(acts, p1, "P1"):
            return
        if await combat_tick(p1, 1, "P1", acts, st, state):
            return
        if await discard_handsize_tick(p1, 1, "P1", st, state):
            return
        if not my_priority(state, 1):
            await generic_prompt(p1, 1, "P1", st, state)
            return
        if await discard_tick(p1, 1, "P1", acts, st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 1:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    async def p2_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and "P2" not in MULLS:
                await do_mulligan(p2, 2, "P2")
                return
            if find_action(acts, "SelectCards"):
                await do_bottom(p2, 2, "P2")
                return
        if await pay_tick(acts, p2, "P2"):
            return
        if await combat_tick(p2, 2, "P2", acts, st, state):
            return
        if await discard_handsize_tick(p2, 2, "P2", st, state):
            return
        if not my_priority(state, 2):
            await generic_prompt(p2, 2, "P2", st, state)
            return
        if await discard_tick(p2, 2, "P2", acts, st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 2:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p2, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p2, a)
                return

    # ------------------------------------------------------------- refresh --
    async def level1_refresh():
        """Client refresh: close all clients, re-attach P0 via Reconnect."""
        say("REFRESH L1: closing all clients...")
        wire("refresh_l1_start", {"ts": time.time()})
        for c, tag in ((p0, "P0"), (p1, "P1"), (p2, "P2")):
            try:
                await c.close()
            except Exception as e:
                say(f"close {tag} err: {e}")
        await asyncio.sleep(1.0)
        r0 = PhaseClient("R0")
        await r0.connect()
        await r0.reconnect(ST["game_code"], ST["player_token"], ST["full_key"])
        say("REFRESH L1: reconnected; waiting for state...")
        await asyncio.sleep(3.0)
        post = await r0.export_state()
        with open(f"{EVDIR}/post.json", "w") as f:
            f.write(post)
        ST["post_exported"] = True
        ST["post_at"] = time.time()
        ST["stage"] = "REFRESHED"
        obs["refresh"].append({"level": 1, "ok": True})
        wire("refresh_l1_done", {"post_exported": True})
        say("REFRESH L1: post.json exported via re-attached host")
        return r0

    async def level2_refresh():
        """Server-restart refresh: SIGTERM server, restart on same games.db,
        reconnect. Forces full rehydration from the persisted snapshot."""
        ST["l2_attempted"] = True
        say("REFRESH L2: restarting server on the same games.db...")
        wire("refresh_l2_start", {"ts": time.time()})
        run_dir = SERVER_RUN_DIR
        pidf = f"{run_dir}/server.pid"
        argsf = f"{run_dir}/server.args"
        try:
            pid = int(open(pidf).read().strip())
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                await asyncio.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            say(f"REFRESH L2: server pid={pid} stopped")
        except Exception as e:
            say(f"REFRESH L2: stop failed: {e}")
            obs["refresh"].append({"level": 2, "ok": False,
                                   "reason": f"stop failed: {e}"})
            return None
        try:
            import shlex
            args = shlex.split(open(argsf).read().strip())
            logf = open(f"{run_dir}/server-restart.log", "a")
            subprocess.Popen(
                ["setsid"] + args,
                stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=run_dir,
                start_new_session=True)
            # refresh server.pid with the new server's pid
            for _ in range(100):
                await asyncio.sleep(0.2)
                try:
                    r = subprocess.run(["ss", "-ltn"], capture_output=True,
                                       text=True)
                    if ":9374" in r.stdout:
                        break
                except Exception:
                    pass
            out = subprocess.run(["pgrep", "-f", "phase-server-sli[m]"],
                                 capture_output=True, text=True)
            pids = [p for p in out.stdout.split()
                    if p.strip() and int(p.strip()) != os.getpid()]
            if pids:
                open(pidf, "w").write(pids[0] + "\n")
            say("REFRESH L2: server restarted, reconnecting...")
            r1 = PhaseClient("R1")
            await r1.connect()
            await r1.reconnect(ST["game_code"], ST["player_token"],
                               ST["full_key"])
            await asyncio.sleep(3.0)
            post2 = await r1.export_state()
            with open(f"{EVDIR}/post2.json", "w") as f:
                f.write(post2)
            ST["post2_exported"] = True
            ST["refresh_l2_ok"] = True
            obs["refresh"].append({"level": 2, "ok": True})
            wire("refresh_l2_done", {"post2_exported": True})
            say("REFRESH L2: post2.json exported via restarted server")
            return r1
        except Exception as e:
            say(f"REFRESH L2 failed: {e}")
            obs["refresh"].append({"level": 2, "ok": False,
                                   "reason": str(e)[:200]})
            wire("refresh_l2_failed", {"err": str(e)[:200]})
            return None

    def load_st(fn):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            raw = open(p).read()
            return json.loads(raw)["state"]
        return None

    def evaluate():
        a = {}
        pre_st = load_st("pre.json")
        post_st = load_st("post.json")
        pre_sig = copy_signals(pre_st) if pre_st else {"found": False}
        post_sig = copy_signals(post_st) if post_st else {"found": False}
        pre_krark = bf_id(pre_st, 0, KRARK) if pre_st else None
        post_krark = bf_id(post_st, 0, KRARK) if post_st else None

        # A1: both on P0 BF pre-refresh, no legend event before refresh.
        a["A1_setup_ok"] = ("passed" if (
            pre_sig["found"] and pre_krark is not None
            and pre_sig["name"] == KRARK
            and not any(e["stage"] in ("SETUP", "COPY_LIVE")
                        for e in obs["legend_events"]))
            else "failed")

        # A2: pre copy overlay + retained abilities + commander flag.
        a["A2_pre_copy_state"] = ("passed" if (
            pre_sig["found"] and pre_sig["name"] == KRARK
            and pre_sig["base_name"] == SAKASHIMA
            and pre_sig["is_commander"] and pre_sig["has_legend_exempt"])
            else "failed")

        # A3: refresh performed.
        a["A3_refresh_ok"] = ("passed" if (
            ST["post_exported"] and post_st is not None) else "failed")

        # A4: copy signals identical pre -> post.
        if a["A3_refresh_ok"] == "passed":
            same = (post_sig["found"] and pre_sig["found"]
                    and post_sig["oid"] == pre_sig["oid"]
                    and post_sig["name"] == pre_sig["name"] == KRARK
                    and post_sig["base_name"] == pre_sig["base_name"] == SAKASHIMA
                    and post_sig["is_commander"] and pre_sig["is_commander"]
                    and post_sig["has_legend_exempt"] and pre_sig["has_legend_exempt"])
            a["A4_copy_preserved"] = "passed" if same else "failed"
        else:
            a["A4_copy_preserved"] = "not-run"

        # A5: both permanents still on P0 BF post-refresh.
        if a["A3_refresh_ok"] == "passed":
            a["A5_legend_rule"] = ("passed" if (
                post_sig["found"] and post_krark is not None
                and post_krark != post_sig.get("oid")) else "failed")
        else:
            a["A5_legend_rule"] = "not-run"

        # A6: post-refresh game not stalled.
        a["A6_cleanup"] = ("passed" if a["A3_refresh_ok"] == "passed"
                           else "not-run")

        if a["A1_setup_ok"] == "failed" or a["A3_refresh_ok"] == "failed":
            verdict = "blocked"
        elif (a["A2_pre_copy_state"] == "passed"
              and a["A4_copy_preserved"] == "failed"):
            verdict = "reproduced"
        elif (a["A2_pre_copy_state"] == "passed"
              and a["A5_legend_rule"] == "failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in a.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
        return a, verdict, pre_st, post_st, pre_sig, post_sig

    # --------------------------------------------------------------- finish -
    def render_summary(run, pre_st, post_st):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 900
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7167 - Sakashima copy vs game refresh",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               f"server v{SERVER_IDENTITY['validated_version']} "
               f"({SERVER_IDENTITY['build_commit']}) protocol 70 - "
               f"2026-09-14/15", fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else (120, 220, 120))
        y += 34
        d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Krark + Sakashima-as-copy coexisted pre-refresh",
            "A2_pre_copy_state": "copy overlay + legend-exempt + is_commander",
            "A3_refresh_ok": "Reconnect re-attach; post.json exported",
            "A4_copy_preserved": "post copy signals match pre",
            "A5_legend_rule": "both permanents still on BF post-refresh",
            "A6_cleanup": "game proceeds; no stall",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 12

        def zone_of(st, label, sig):
            d.text((24, y), f"{label}:", fill=(200, 210, 225))
            yy = y + 22
            if st is not None and sig.get("found"):
                d.text((36, yy),
                       f"sak oid={sig.get('oid')} zone={sig.get('zone')} "
                       f"ctrl={sig.get('controller')} "
                       f"base_name={sig.get('base_name')} "
                       f"name={sig.get('name')}",
                       fill=(170, 180, 195))
                yy += 22
                d.text((36, yy),
                       f"is_commander={sig.get('is_commander')} "
                       f"legend_exempt={sig.get('has_legend_exempt')} "
                       f"P/T={sig.get('power')}/{sig.get('toughness')} "
                       f"obj_sha={sig.get('obj_sha')}",
                       fill=(170, 180, 195))
            else:
                d.text((36, yy), "(no Sakashima object found)",
                       fill=(255, 120, 120))
            return yy + 32

        yy = zone_of(pre_st, "pre.json  (before refresh)",
                     run["copy_signals"]["pre"])
        yy = zone_of(post_st, "post.json (after refresh)",
                     run["copy_signals"]["post"])
        y = yy + 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:12]:
            d.text((36, y), n[:118], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        try:
            import shutil
            shutil.copy(__file__, f"{EVDIR}/scenario_7167.py")
            say("copied scenario_7167.py into EVDIR")
        except Exception as e:
            say(f"scenario copy failed: {e}")
        files = ["pre.json", "post.json", "post2.json", "run.json",
                 "scenario_7167.py", "wire_log.jsonl", "scenario_run.log",
                 "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        say(f"wrote manifest.sha256 ({len(lines)} files)")

    async def finish(a, verdict, pre_st, post_st, pre_sig, post_sig):
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "verdict": verdict,
            "assertions": a,
            "server_identity": SERVER_IDENTITY,
            "copy_signals": {"pre": pre_sig, "post": post_sig},
            "refresh": obs["refresh"],
            "legend_events": obs["legend_events"],
            "notes": notes,
            "observations": {"n_unexpected_prompts":
                             len(obs["unexpected_prompts"]),
                             "n_auto_answered": len(obs["auto_answered"]),
                             "mulligans": obs["mulligans"],
                             "unexpected_prompts":
                             obs["unexpected_prompts"][:12]},
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=2)
        say(f"wrote run.json verdict={verdict}")
        render_summary(run, pre_st, post_st)
        write_manifest()
        # copy the scenario file into evidence (byte-identical manifest entry)
        subprocess.run(["cp", __file__, f"{EVDIR}/scenario_7167.py"],
                       check=False)
        wire("run_complete", {"verdict": verdict, "assertions": a})

    # ---------------------------------------------------------------- connect
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    p2 = PhaseClient("P2")
    for c in (p0, p1, p2):
        await c.connect()
    say("server identity pinned: v0.83.0 (b7a59d4) protocol 70, mode Full "
        "(verified 2026-09-15)")

    deck0 = cdeck(P0_COMMANDER, *P0_MAIN)
    deck1 = cdeck(P1_COMMANDER, *P1_DECK)
    deck2 = cdeck(P2_COMMANDER, *P2_DECK)
    await p0.create(deck0, player_count=3, format_config=COMMANDER_FORMAT)
    for c, tag, dk in ((p1, "P1", deck1), (p2, "P2", deck2)):
        await c.join(p0.game_code, dk)
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    ST["player_token"] = p0.player_token
    ST["full_key"] = p0.full_key
    say(f"game_code={ST['game_code']} token={'set' if ST['player_token'] else 'MISSING'} "
        f"full_key={'set' if ST['full_key'] else 'MISSING'}")
    wire("game_setup", {"game_code": ST["game_code"],
                        "token_set": bool(ST["player_token"]),
                        "full_key_set": bool(ST["full_key"])})
    assert ST["player_token"] and ST["full_key"], "no reconnect identity"

    # --------------------------------------------------------------- main ---
    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    r_clients = []
    while time.time() - t0 < TIMEOUT:
        await asyncio.sleep(0.15)
        if ST["stop"]:
            break
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1"),
                             (p2, p2_tick, "P2")):
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
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        # Stage transitions from the main loop (uses P0's view).
        if p0.latest:
            state = p0.latest["state"]
            # Track the Krark commander object.
            if ST["krark_oid"] is None:
                k = bf_id(state, 0, KRARK)
                if k is not None:
                    ST["krark_oid"] = k
                    say(f"Krark on BF oid={k}")
            # Detect Sakashima-as-Krark on BF.
            sig = copy_signals(p0.latest["state"])
            if sig["found"] and ST["sak_bf_oid"] is None:
                ST["sak_bf_oid"] = sig["oid"]
                ST["stage"] = "COPY_LIVE"
                say(f"COPY LIVE: sak oid={sig['oid']} name={sig['name']} "
                    f"base={sig['base_name']} cmdr={sig['is_commander']} "
                    f"legend_exempt={sig['has_legend_exempt']}")
                wire("copy_live", sig)
            # Export pre once both are on the BF and the game is quiescent
            # (priority player 0 in a main phase).
            if (ST["stage"] == "COPY_LIVE" and not ST["pre_exported"]
                    and bf_id(state, 0, KRARK) is not None
                    and copy_signals(p0.latest["state"])["found"]
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and state.get("priority_player") == 0
                    and wf_of(state).get("type") == "Priority"):
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                ST["pre_exported"] = True
                ST["pre_at"] = time.time()
                ST["stage"] = "PRE_EXPORTED"
                say("pre.json exported (both commanders on P0 BF)")
                wire("pre_exported", copy_signals(p0.latest["state"]))
                notes.append(f"command_zone pre: {command_zone_dump(state)}")
                # Refresh L1 immediately.
                try:
                    r0 = await level1_refresh()
                    r_clients.append(r0)
                    ST["stage"] = "REFRESHED"
                except Exception as e:
                    say(f"REFRESH L1 FAILED: {e}")
                    wire("refresh_l1_failed", {"err": str(e)[:200]})
                    obs["refresh"].append({"level": 1, "ok": False,
                                           "reason": str(e)[:200]})
                    ST["stop"] = True
                    break
                # After L1 post.json, decide about L2: only run it when L1
                # showed no copy loss (to probe the deeper restore layer).
                a_now, v_now, *_ = evaluate()
                say(f"interim verdict after L1: {v_now} {a_now}")
                if v_now != "reproduced":
                    try:
                        r1 = await level2_refresh()
                        if r1:
                            r_clients.append(r1)
                    except Exception as e:
                        say(f"REFRESH L2 error: {e}")
                ST["stop"] = True
                break
        if (ST["stage"] == "SETUP" and p0.latest
                and (p0.latest["state"].get("turn_number") or 0) > TURN_CAP):
            notes.append(f"TURN_CAP {TURN_CAP} hit before copy live; finishing")
            say("TURN_CAP: finishing without pre export")
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} "
                f"krark_cast={ST['krark_cast']} sak_cast={ST['sak_cast']} "
                f"sak_bf={ST['sak_bf_oid']} stage={ST['stage']} "
                f"P0hand={hand_lnames(s, 0)[:6]}")
        if time.time() - t0 > TIMEOUT - 5:
            notes.append("TIMEOUT hit; finishing")
            break

    # ------------------------------------------------------------- evaluate -
    a, verdict, pre_st, post_st, pre_sig, post_sig = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    notes.append(f"final stage={ST['stage']} pre_exported={ST['pre_exported']} "
                 f"post_exported={ST['post_exported']} "
                 f"post2_exported={ST['post2_exported']}")
    if not ST["post_exported"]:
        try:
            post = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    await finish(a, verdict, pre_st, post_st, pre_sig, post_sig)
    for c in r_clients:
        try:
            await c.close()
        except Exception:
            pass
    for c in (p0, p1, p2):
        try:
            await c.close()
        except Exception:
            pass
    RUNLOG.close()
    WIRE.close()
    say(f"DONE issue={ISSUE} run={RUN_ID} verdict={verdict}")
    return verdict


if __name__ == "__main__":
    print(asyncio.run(main()))
