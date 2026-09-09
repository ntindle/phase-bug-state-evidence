#!/usr/bin/env python3
"""Issue #3919: Stuck decision: ReplacementChoice.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.2.1 2fe10e0, 2026-06-20):
  "Was playing Mirage Mesa from the Graveyard due to Titania, Protector of
   Argoth's ETB. Game froze as Mirage was selected. Not entirely sure what
   caused the error."
  Diagnostic: Waiting for: ReplacementChoice | Stuck players: 0

Oracle text (pinned card-data.json v0.78.0):
  Titania, Protector of Argoth: "When Titania enters, return target land card
    from your graveyard to the battlefield. Whenever a land you control is put
    into a graveyard from the battlefield, create a 5/3 green Elemental
    creature token."
  Mirage Mesa: "This land enters tapped. As it enters, choose a color.
    {T}: Add one mana of the chosen color."

Expected behavior: Titania's ETB trigger targets Mirage Mesa in the graveyard;
Mesa enters the battlefield; its two as-enters replacements resolve - the
enter-tapped clause applies automatically and the controller is offered a real
color choice (5 colors); the game then proceeds. No stall.

The reported failure: the game freezes at ReplacementChoice with Stuck
players: 0 - i.e. a replacement-choice decision with no actionable submission.
A 2026-07-26 comment attributes this to the zero-candidate/absent
replacement-choice softlock guarded by PR #4306 (6bf164900); this run tests
whether that signature still occurs on v0.78.0 (4de7224).

Setup: P0 (8x Titania / 8x Mirage Mesa / 8x Faithless Looting / 20x Forest /
16x Mountain). P0 mulligans to Titania + Looting + 2 lands, casts Looting
turn 2 (draw 2, discard Mesa + 1), casts Titania once 5 mana with 2 green is
available, targets Mesa with the ETB trigger, then answers the entry
replacements. P1 is a do-nothing Forest deck.

Assertions:
  A1 setup_ok ......... P0 casts Titania with Mirage Mesa in its graveyard;
                        pre.json exported (ETB trigger on stack, Mesa in gy).
  A2 etb_targets_mesa . Titania's ETB trigger targeted Mirage Mesa (submitted
                        target or auto-target path recorded).
  A3 no_stall ......... no ReplacementChoice wait with zero candidates / no
                        legal submission; the 60s stall watchdog never fired.
  A4 color_choice ..... a real color choice (>=2 candidates) was offered to
                        P0 for Mesa's entry and answered (White).
  A5 mesa_battlefield . post.json: Mirage Mesa on P0's battlefield, tapped,
                        with the chosen color recorded.
  A6 cleanup .......... post.json: stack empty, game proceeding.

Verdict rule:
  reproduced .... the reported stall signature is observed: a ReplacementChoice
                  wait with no actionable submission (zero candidates / no
                  legal actions / no submittable viewer interaction) persisting
                  >60s, Mesa never entering; or the related failure where the
                  ETB resolves but Mesa never enters the battlefield (>150s).
  not-reproduced  the entry replacements resolve per Oracle and the game
                  proceeds (A3+A5+A6 pass; A4 deviation noted if the choice
                  was auto-resolved without an offer).
  blocked ....... the setup gate never opens (Titania never castable / Mesa
                  never reaches the graveyard); no engine behavior observed.

Evidence: evidence/3919/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_3919.py, wire_log.jsonl,
scenario_run.log (+ mid_stall.json only if the stall fires).
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-3919"
EVDIR = f"{BACKFILL}/evidence/3919/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TITANIA = "Titania, Protector of Argoth"
MESA = "Mirage Mesa"
LOOTING = "Faithless Looting"
FOREST = "Forest"
MOUNTAIN = "Mountain"

P0_DECK = [(TITANIA, 8), (MESA, 8), (LOOTING, 8), (FOREST, 20), (MOUNTAIN, 16)]
P1_DECK = [(FOREST, 60)]

COLORS = {"white", "blue", "black", "red", "green", "w", "u", "b", "r", "g"}

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
    "source": "sha256 match of pinned verified release artifacts (verified 2026-09-09 run, ledger); "
              "fresh isolated server on 127.0.0.1:9374 from this run's run dir",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def obj_name(state, oid):
    o = get_obj(state, oid)
    return o.get("base_name") or o.get("name") or "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def gy_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("graveyard", [])]


def bf_ids(state, pid, name=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (name is None or lname(state, oid) == name.lower())]


LAND_NAMES = {FOREST.lower(), MOUNTAIN.lower(), MESA.lower()}


def untapped_lands(state, pid):
    return [o for o in bf_ids(state, pid)
            if not get_obj(state, o).get("tapped")
            and lname(state, o) in LAND_NAMES]


def untapped_forests(state, pid):
    return [o for o in untapped_lands(state, pid)
            if lname(state, o) == FOREST.lower()]


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_etb_targets_mesa", "A3_no_stall",
            "A4_color_choice", "A5_mesa_battlefield", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    pre_exported = False
    post_exported = False
    looting_casts = 0
    titania_cast = False
    settle_empty = 0

    obs = {
        "target_submitted": None,      # 'mesa' | 'auto' | None
        "target_path": None,           # 'prompt' | 'auto' | None
        "color_offered": False,
        "color_candidates": 0,
        "color_chosen": None,
        "stall_observed": False,
        "replacement_waits": [],       # every ReplacementChoice wait snapshot
        "rejections": [],
        "mesa_entry_turn": None,
    }
    shapes_logged = set()
    submitted_iids = set()
    repl_wait_start = None

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def mesa_gy_oid(state):
        ids = [o for o in gy_ids(state, 0) if lname(state, o) == MESA.lower()]
        return ids[0] if ids else None

    def mesa_on_bf(state):
        ids = [o for o in bf_ids(state, 0) if lname(state, o) == MESA.lower()]
        return ids[0] if ids else None

    def titania_on_bf(state):
        ids = [o for o in bf_ids(state, 0) if lname(state, o) == TITANIA.lower()]
        return ids[0] if ids else None

    async def scan_interactions(st, c, tag, pid, state):
        """Handle Titania ETB target selection and Mesa's choose-a-color.
        Returns True if an interaction was submitted."""
        nonlocal pre_exported
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
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            key = (tag, rtype, spec_type, blob[:100])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{tag}] interaction rtype={rtype} spec={spec_type} "
                    f"n_choices={len(chs)} choices=[{blob[:200]}]")
                wire("interaction_shape", {"who": tag, "rtype": rtype,
                                           "spec_type": spec_type,
                                           "interaction": opp})
            if iid in submitted_iids:
                continue
            # --- color choice (Mesa's "choose a color"): schema/text spec,
            # candidates carry the color in surfaces[].data.value; the
            # opportunity's own surfaces name Mirage Mesa as the source.
            # Submit {"type":"text","data":{"value":"<color>"}} per the
            # engine's InteractionResponse::Text shape (value must be one of
            # the offered options when allow_arbitrary=false).
            color_chs = [ch for ch in chs if choice_text(ch).lower() in COLORS]
            if (len(color_chs) >= 2 and "mirage mesa" in low
                    and rtype == "schema" and spec_type == "text"):
                obs["color_offered"] = True
                obs["color_candidates"] = len(color_chs)
                white = next((ch for ch in color_chs
                              if choice_text(ch).lower() == "white"), color_chs[0])
                value = choice_text(white)
                sub = {"interactionId": iid, "response":
                       {"type": "text", "data": {"value": value}}}
                say(f"[{tag}] color choice: answering {value} "
                    f"(candidates={len(color_chs)})")
                wire("color_choice_submission", {"who": tag, "submission": sub})
                obs["color_chosen"] = value
                await c.send_interaction(sub)
                submitted_iids.add(iid)
                acted = True
                continue
            # --- target selection (schema only; exactChoices menus are not targets) ---
            if rtype == "schema":
                mesa_ch = next((ch for ch in chs
                                if "mirage mesa" in choice_text(ch).lower()), None)
                if mesa_ch is not None and titania_on_bf(state) is not None:
                    cid = mesa_ch.get("id")
                    sub = {"interactionId": iid, "response":
                           {"type": spec_type or "sequence",
                            "data": {"choiceIds": [cid]}}}
                    say(f"[{tag}] ETB target: selecting Mirage Mesa")
                    wire("etb_target_submission",
                         {"who": tag, "submission": sub,
                          "candidate": {k: mesa_ch.get(k)
                                        for k in ("id", "text", "label", "name")}})
                    obs["target_submitted"] = "mesa"
                    obs["target_path"] = "prompt"
                    obs["target_submitted_at"] = time.time()
                    await c.send_interaction(sub)
                    submitted_iids.add(iid)
                    acted = True
                    # pre-export: trigger on stack, Mesa still in graveyard
                    try:
                        pre = await p0.export_state()
                        with open(f"{EVDIR}/pre.json", "w") as f:
                            f.write(pre)
                        pre_st = json.loads(pre)["state"]
                        say(f"PRE exported: Titania BF, Mesa gy="
                            f"{mesa_gy_oid(pre_st) is not None}")
                        wire("pre_export", {"mesa_in_gy": mesa_gy_oid(pre_st) is not None,
                                            "titania_bf": titania_on_bf(pre_st) is not None})
                    except Exception as e:
                        notes.append(f"pre export failed: {e}")
                    pre_exported = True
                    continue
                # unknown schema prompt: log but do not invent a submission
                wire("unhandled_schema_prompt", {"who": tag, "choices": texts[:10]})
        return acted

    async def mulligan_tick(c, pid, acts, state, tag):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_names(state, pid)
            if tag == "P0":
                has_t = TITANIA.lower() in hn
                has_l = LOOTING.lower() in hn
                lands = sum(1 for n in hn if n in (FOREST.lower(), MOUNTAIN.lower()))
                mulls = kept.get(f"{tag}_mulls", 0)
                if (has_t and has_l and lands >= 2) or mulls >= 3:
                    kept[tag] = True
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Keep"}}})
                    say(f"{tag} keeps (titania={has_t}, looting={has_l}, lands={lands})")
                else:
                    kept[f"{tag}_mulls"] = mulls + 1
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Mulligan"}}})
                    say(f"{tag} mulligans #{mulls + 1}")
            else:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps 7")
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{tag}_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, pid)
                if tag == "P0":
                    seen = {}
                    def bottom_key(oid):
                        nm = lname(state, oid)
                        if nm in (FOREST.lower(), MOUNTAIN.lower()):
                            # keep 2 lands, bottom extras first
                            k = seen.get("land", 0)
                            seen["land"] = k + 1
                            return (0 if k >= 2 else 3, nm)
                        for key, want in ((TITANIA.lower(), 1), (LOOTING.lower(), 1),
                                          (MESA.lower(), 1)):
                            if nm == key:
                                kk = seen.get(key, 0)
                                seen[key] = kk + 1
                                return (1 if kk >= want else 3, nm)
                        return (2, nm)
                    picks = sorted(hids, key=bottom_key)[:count]
                else:
                    picks = hids[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def generic_decision(c, pid, tag, acts, state, wtype):
        """Non-priority waiting_for handling shared by both seats."""
        # discard (Faithless Looting): Mesa first, then lands
        if "Discard" in wtype:
            hids = hand_ids(state, pid)
            def discard_rank(oid):
                nm = lname(state, oid)
                if nm == MESA.lower():
                    return (0, nm)
                if nm in (FOREST.lower(), MOUNTAIN.lower()):
                    return (1, nm)
                return (2, nm)
            hids.sort(key=discard_rank)
            n = 2
            d = (state.get("waiting_for") or {}).get("data", {})
            for k in ("count", "amount", "number"):
                if isinstance(d.get(k), int):
                    n = d[k]
            picks = hids[:n]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{tag} discards {[lname(state, x) for x in picks]}")
            wire("discard", {"who": tag,
                             "cards": [lname(state, x) for x in picks]})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
                say(f"{tag} submits advertised OrderTriggers")
                return True
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                dd = sub.setdefault("data", {})
                for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                    if k in dd:
                        dd[k] = [] if isinstance(dd[k], list) else {}
                await c.send_action({"type": wtype, "data": dd})
                say(f"{tag} declares empty {wtype}")
                return True
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say(f"{tag} submits advertised AssignCombatDamage")
                return True
        return False

    def stack_signatures(state):
        out = []
        for e in state.get("stack", []) or []:
            keys = sorted(e.keys())
            eff = e.get("effect") or e.get("effects") or {}
            out.append({"keys": keys,
                        "id": e.get("id"),
                        "source": e.get("source") or e.get("source_id"),
                        "effect_keys": sorted(eff.keys()) if isinstance(eff, dict) else type(eff).__name__})
        return out

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, looting_casts, titania_cast
        nonlocal repl_wait_start, settle_empty
        wtype = (state.get("waiting_for") or {}).get("type") or ""
        if await mulligan_tick(p0, 0, acts, state, "P0"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # --- ReplacementChoice stall tracking (the reported signature) ---
        if "ReplacementChoice" in wtype:
            vi = get_vi(st)
            actionable = vi is not None or any(
                a["type"] not in ("PassPriority",) for a in acts)
            rec = {"turn": state.get("turn_number"), "phase": state.get("phase"),
                   "revision": p0.revision, "actionable": actionable,
                   "n_legal": len(acts),
                   "vi_can_submit": vi is not None}
            if not obs["replacement_waits"] or \
                    obs["replacement_waits"][-1]["revision"] != rec["revision"]:
                obs["replacement_waits"].append(rec)
                say(f"P0 ReplacementChoice wait rev={rec['revision']} "
                    f"actionable={actionable} n_legal={len(acts)}")
                wire("replacement_wait", rec)
            if repl_wait_start is None:
                repl_wait_start = time.time()
            if not actionable and time.time() - repl_wait_start > 60:
                obs["stall_observed"] = True
                say("STALL: ReplacementChoice >60s with no actionable submission")
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_stall.json", "w") as f:
                        f.write(mid)
                except Exception as e:
                    notes.append(f"mid_stall export failed: {e}")
                return
        else:
            repl_wait_start = None
        # --- interactions (ETB target + color choice) ---
        if await scan_interactions(st, p0, "P0", 0, state):
            return
        # --- auto-target fallback: ETB trigger on stack, Mesa in gy, no prompt ---
        if (not pre_exported and titania_cast and mesa_gy_oid(state) is not None
                and titania_on_bf(state) is not None
                and len(state.get("stack", []) or []) > 0):
            if "stack_logged" not in shapes_logged:
                shapes_logged.add("stack_logged")
                wire("stack_signatures", stack_signatures(state))
            obs["target_submitted"] = "mesa"
            obs["target_path"] = "auto"
            obs["target_submitted_at"] = time.time()
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_exported = True
                say("PRE exported (auto-target path: ETB trigger on stack, Mesa in gy)")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
            return
        # --- post checkpoint: Titania path complete - Mesa entered via the ETB,
        # stack empty and settling ---
        if (not post_exported and titania_cast
                and obs["target_submitted"] == "mesa"
                and mesa_on_bf(state) is not None):
            if len(state.get("stack", []) or []) == 0:
                settle_empty += 1
            else:
                settle_empty = 0
            if settle_empty >= 3:
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("POST exported (Mesa on battlefield, stack settled)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                return
        if await generic_decision(p0, 0, "P0", acts, state, wtype):
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        phase = state.get("phase")
        hn = hand_names(state, 0)
        # cast Faithless Looting (up to twice, until Mesa is in the graveyard)
        if (looting_casts < 2 and mesa_gy_oid(state) is None
                and LOOTING.lower() in hn
                and phase in ("PreCombatMain", "PostCombatMain")
                and any(not get_obj(state, o).get("tapped") and
                        lname(state, o) == MOUNTAIN.lower()
                        for o in bf_ids(state, 0))):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and \
                        lname(state, d.get("object_id")) == LOOTING.lower():
                    say(f"P0 casts Faithless Looting (#{looting_casts + 1})")
                    wire("cast_looting", a)
                    await submit_as_is(p0, a)
                    looting_casts += 1
                    return
        # cast Titania once Mesa is binned and mana is ready
        if (not titania_cast and mesa_gy_oid(state) is not None
                and TITANIA.lower() in hn
                and phase in ("PreCombatMain", "PostCombatMain")):
            nl = len(untapped_lands(state, 0))
            nf = len(untapped_forests(state, 0))
            if nl >= 5 and nf >= 2:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and \
                            lname(state, d.get("object_id")) == TITANIA.lower():
                        say("P0 casts Titania, Protector of Argoth")
                        wire("cast_titania", a)
                        await submit_as_is(p0, a)
                        titania_cast = True
                        obs["mesa_entry_turn"] = state.get("turn_number")
                        return
            elif time.time() - obs.get("last_mana_dbg", 0) > 30:
                obs["last_mana_dbg"] = time.time()
                say(f"[P0] Titania in hand, Mesa binned, waiting on mana: "
                    f"untapped_lands={nl} forests={nf}")
        for a in acts:
            if a["type"] == "PlayLand":
                oid = a.get("data", {}).get("object_id")
                # never play Mesa as a land drop: the contract needs Mesa to
                # enter via Titania's ETB from the graveyard (8x Mesa in deck
                # plus 36 other lands, so mana is not a concern)
                if oid is not None and lname(state, oid) == MESA.lower():
                    continue
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        # fallback: pass via viewer_interaction exactChoices menu
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                resp = opp.get("response", {}) or {}
                if resp.get("type") != "exactChoices":
                    continue
                chs = resp.get("data", {}).get("choices", []) or []
                pc = next((ch for ch in chs
                           if "pass" in choice_text(ch).lower()), None)
                if pc is not None and opp.get("interactionId") not in submitted_iids:
                    sub = {"interactionId": opp["interactionId"], "response":
                           {"type": "choose", "data": {"choiceId": pc.get("id")}}}
                    say("P0 passes priority via viewer_interaction fallback")
                    await p0.send_interaction(sub)
                    submitted_iids.add(opp["interactionId"])
                    return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type") or ""
        if await mulligan_tick(p1, 1, acts, state, "P1"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if await scan_interactions(st, p1, "P1", 1, state):
            return
        if await generic_decision(p1, 1, "P1", acts, state, wtype):
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

    def mesa_snapshot(state, oid):
        o = get_obj(state, oid)
        return {k: o.get(k) for k in
                ("zone", "tapped", "controller", "owner", "base_name", "name",
                 "color", "colors", "base_color", "chosen_color", "choice",
                 "chosen_attributes", "enters_tapped", "type_line")}

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish (not the clean checkpoint)")
            except Exception as e:
                notes.append(f"final post export failed: {e}")
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")

        # A1: Titania cast with Mesa in gy; pre exported with that precondition
        if pre_st is not None and titania_cast and mesa_gy_oid(pre_st) is not None \
                and titania_on_bf(pre_st) is not None:
            ass["A1_setup_ok"] = "passed"
            notes.append("A1 passed: pre.json shows Titania on P0 battlefield and "
                         "Mirage Mesa in P0 graveyard with the ETB trigger pending")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(f"A1 failed: titania_cast={titania_cast}, "
                         f"pre_exists={pre_st is not None}, "
                         f"mesa_gy_pre={mesa_gy_oid(pre_st) is not None if pre_st else None}")
        # A2: ETB targeted Mesa
        if obs["target_submitted"] == "mesa":
            ass["A2_etb_targets_mesa"] = "passed"
            notes.append(f"A2 passed: ETB target = Mirage Mesa "
                         f"(path={obs['target_path']})")
        else:
            ass["A2_etb_targets_mesa"] = "failed"
            notes.append("A2 failed: no Mesa target submission/auto-target recorded")
        # A3: no stall
        if obs["stall_observed"]:
            ass["A3_no_stall"] = "failed"
            notes.append(f"STALL: ReplacementChoice wait with no actionable "
                         f"submission persisted >60s "
                         f"({len(obs['replacement_waits'])} waits logged)")
        elif obs["replacement_waits"]:
            ass["A3_no_stall"] = "passed"
            notes.append(f"A3 passed: {len(obs['replacement_waits'])} "
                         f"ReplacementChoice wait(s) observed, every one actionable, "
                         f"none stalled")
        else:
            ass["A3_no_stall"] = "passed"
            notes.append("A3 passed: no ReplacementChoice wait ever needed a stall "
                         "watchdog (entry replacements resolved without parking)")
        # A4: color choice offered
        if obs["color_offered"] and obs["color_candidates"] >= 2:
            ass["A4_color_choice"] = "passed"
            notes.append(f"A4 passed: color choice offered with "
                         f"{obs['color_candidates']} candidates; "
                         f"answered {obs['color_chosen']}")
        elif mesa_on_bf(post_st) is not None if post_st else False:
            ass["A4_color_choice"] = "failed"
            notes.append("A4 failed: Mesa entered but no color-choice opportunity "
                         "with >=2 candidates was ever offered to P0")
        else:
            ass["A4_color_choice"] = "failed"
            notes.append("A4 failed: no color choice offered and Mesa never entered")
        # A5: Mesa on battlefield, tapped, color recorded
        if post_st is not None:
            moid = mesa_on_bf(post_st)
            if moid is not None:
                snap = mesa_snapshot(post_st, moid)
                wire("mesa_post_snapshot", snap)
                # the engine records the as-enters color choice in
                # chosen_attributes ([{type: Color, value: <color>}]); the
                # `color` field stays [] for a colorless land
                chosen_attrs = snap.get("chosen_attributes") or []
                color_recorded = any(
                    isinstance(a, dict) and a.get("type") == "Color"
                    and str(a.get("value", "")).lower()
                    == (obs["color_chosen"] or "").lower()
                    for a in chosen_attrs)
                color_fields = {k: v for k, v in snap.items()
                                if k in ("color", "colors", "base_color",
                                         "chosen_color", "choice",
                                         "chosen_attributes") and v}
                if snap.get("tapped"):
                    ok = bool(color_fields) and (
                        not obs["color_chosen"] or color_recorded)
                    ass["A5_mesa_battlefield"] = "passed" if ok else "failed"
                    notes.append(f"A5 {'passed' if ok else 'failed'}: "
                                 f"Mesa oid {moid} on P0 battlefield, tapped=True, "
                                 f"color fields={color_fields or 'NONE RECORDED'}")
                else:
                    ass["A5_mesa_battlefield"] = "failed"
                    notes.append(f"A5 failed: Mesa on battlefield but tapped="
                                 f"{snap.get('tapped')} (expected True)")
            else:
                ass["A5_mesa_battlefield"] = "failed"
                notes.append("A5 failed: Mirage Mesa not on P0 battlefield in post.json")
        else:
            ass["A5_mesa_battlefield"] = "failed"
            notes.append("A5 failed: no post.json")
        # A6: cleanup
        if post_st is not None:
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if slen == 0 and not obs["stall_observed"]:
                ass["A6_cleanup"] = "passed"
                notes.append(f"A6 passed: post.json stack empty, game proceeding "
                             f"(turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}, wf={wf})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"A6 failed: post stack={slen}, wf={wf}, "
                             f"stall={obs['stall_observed']}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: no post.json")
        # Verdict
        if obs["stall_observed"] or obs.get("entry_stall"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: the reported ReplacementChoice stall "
                         "signature (no actionable submission, >60s) observed on "
                         "v0.78.0" + ("; additionally the ETB-resolved Mesa never "
                         "entered the battlefield (entry stall, related failure)"
                         if obs.get("entry_stall") else ""))
        elif (ass["A3_no_stall"] == "passed"
              and ass["A5_mesa_battlefield"] == "passed"
              and ass["A6_cleanup"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: Mesa's entry replacements resolved "
                         "per Oracle and the game proceeded; the reported freeze "
                         "does not occur on v0.78.0")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: setup gate never opened or entry did not "
                         "complete; no trustworthy engine behavior observed")
        run = {
            "issue": 3919,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_3919.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x Titania / 8x Mirage Mesa / 8x Faithless Looting deck density is a "
                "test-harness convenience (engine accepts >4-of for custom games).",
                "Not tested on the original 2026-06-20 build v0.2.1; verdict is scoped "
                "to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x Titania + 8x Mirage Mesa + 8x Faithless Looting + 20x Forest + "
                          "16x Mountain (mulligan to Titania+Looting+2 lands; Looting bins Mesa; "
                          "Titania cast with 5 mana/2 green; ETB targets Mesa); "
                          "P1: 60x Forest, do-nothing",
            "contract_line": "Titania ETB targets Mirage Mesa; Mesa enters tapped; its controller "
                             "is offered a real color choice; no ReplacementChoice stall; game proceeds",
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

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stall_return = False
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
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
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        if obs["stall_observed"] and not stall_return:
            stall_return = True
            say("stall signature confirmed; finishing")
            await finish()
            return
        # entry-stall watchdog: ETB target submitted but Mesa never entered
        # within 150s (a related failure: entry replacements never completed)
        if (obs["target_submitted"] and not post_exported
                and obs.get("target_submitted_at")
                and not obs.get("entry_stall")):
            s_now = p0.latest["state"] if p0.latest else None
            if (s_now is not None and mesa_on_bf(s_now) is None
                    and time.time() - obs["target_submitted_at"] > 150):
                obs["entry_stall"] = True
                say("ENTRY STALL: ETB target submitted >150s ago, Mesa never entered")
                wire("entry_stall", {
                    "waiting_for": (s_now.get("waiting_for") or {}).get("type"),
                    "phase": s_now.get("phase"),
                    "stack": stack_signatures(s_now)})
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_entry_stall.json", "w") as f:
                        f.write(mid)
                except Exception as e:
                    notes.append(f"mid_entry_stall export failed: {e}")
                await finish()
                return
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} looting={looting_casts} "
                f"titania_cast={titania_cast} mesa_gy={mesa_gy_oid(s) is not None} "
                f"mesa_bf={mesa_on_bf(s) is not None} "
                f"repl_waits={len(obs['replacement_waits'])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
