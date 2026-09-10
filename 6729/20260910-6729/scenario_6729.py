#!/usr/bin/env python3
"""Issue #6729: Green Sun's Zenith does not trigger ETB (Arboreal Grazer).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-28, status:confirmed, area:engine+parser): casting Green Sun's
Zenith to tutor a creature with an ETB (Arboreal Grazer) onto the battlefield
does not produce the creature's enters trigger. The reporter also noted the
Zenith hitting the graveyard (wrong: it must move from the stack into its
owner's library) and the log showing the library shuffled repeatedly.

Oracle text (pinned v0.78.0 card-data.json, keys verified 2026-09-10):
  Green Sun's Zenith: "Search your library for a green creature card with mana
    value X or less, put it onto the battlefield, then shuffle. Shuffle Green
    Sun's Zenith into its owner's library."
  Arboreal Grazer: "Reach. When this creature enters, you may put a land card
    from your hand onto the battlefield tapped."

Pinned parse note (recorded in parse_evidence.json): the search-following
ChangeZone has target Any rather than the found card (matches the triage
analysis), then Shuffle(Controller), ChangeZone(Self -> owner Library),
Shuffle(ParentTargetOwner). Grazer's trigger parses faithfully as a
ChangesZone/SelfRef optional trigger.

Acceptance criteria (from triage, asserted explicitly here):
  A1 setup_ok        Zenith cast with X=1 (Arboreal Grazer has MV 1: cost {G});
                     the search prompt was offered.
  A2 grazer_bf       The searched Grazer moved onto P0's battlefield.
  A3 etb_triggered   Grazer's enters trigger fired (observed on the stack);
                     driver accepted the optional "you may" and the land from
                     hand entered the battlefield tapped (+1 tapped Forest for
                     P0, hand -1).
  A4 zenith_library  The Zenith moved from the stack into P0's library (not
                     the graveyard).
  A5 shuffle_exactly2 Exactly two library shuffles during the Zenith
                     resolution (the two Oracle instructions), with no
                     duplicate automatic shuffle.
  A6 cleanup         Stack empty, game proceeds.

Verdict rule: reproduced iff A2 or A3 fails with Grazer reaching the BF
without its enters trigger (the reported bug), or A4/A5 fail with the wrong
Zenith destination / extra shuffles (the secondary observations). A prompt
appearing is not a pass; only the completed outcome counts.

Scenario (native engine, two human-client seats):
  P0: 12x Green Sun's Zenith + 12x Arboreal Grazer + 36x Forest. Keeps a hand
      with a Zenith and >=2 Forests, plays two Forests, casts the Zenith with
      X=1 (Grazer MV=1: cost {G}), searches the Grazer, answers its ETB.
  P1: 60x Island. Plays a land per turn, always passes priority, never attacks.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402

client.URL = "ws://127.0.0.1:9377/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6729"
ISSUE = 6729
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ZENITH = "green sun's zenith"
GRAZER = "arboreal grazer"
FOREST = "forest"
ISLAND = "island"
LANDS = (FOREST,)

P0_DECK = [(ZENITH, 12), (GRAZER, 12), (FOREST, 36)]
P1_DECK = [(ISLAND, 60)]

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
    "observed_at": "2026-09-10",
    "source": "ServerHello probed live (0.78.0/4de7224/proto 68/Full) on "
              "127.0.0.1:9377; pinned v0.78.0 release = latest stable "
              "(published 2026-09-09); minisign-verified binary + signed data "
              "manifest with repo-pinned SERVER_ARTIFACT_PUBLIC_KEY; fresh "
              "isolated server owned by this run on 127.0.0.1:9377",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


def lib_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("library", [])]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, name):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and obj_name(o) == name):
            out.append(int(oid))
    return out


def untapped_bf_ids(state, pid, name):
    """Battlefield objects of `name` controlled by pid that are not tapped."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and obj_name(o) == name and not o.get("tapped")):
            out.append(int(oid))
    return out


def zenith_on_stack(state):
    for e in state.get("stack", []) or []:
        blob = json.dumps(e, default=str).lower()
        if "zenith" in blob:
            return e
    return None


def grazer_trigger_on_stack(state, grazer_oid=None):
    """The Grazer ChangesZone trigger: a TriggeredAbility stack entry sourced
    at the Grazer (source_id == grazer oid), or with the ChangeZone
    hand->battlefield/enter_tapped effect signature."""
    for e in state.get("stack", []) or []:
        kind = (e.get("kind") or {})
        if isinstance(kind, dict):
            ktype = kind.get("type")
        else:
            ktype = str(kind)
        if ktype != "TriggeredAbility":
            continue
        if grazer_oid is not None and e.get("source_id") is not None:
            try:
                if int(e["source_id"]) == int(grazer_oid):
                    return e
            except (TypeError, ValueError):
                pass
        blob = json.dumps(e, default=str).lower()
        if "arboreal grazer" in blob or ("changezone" in blob and "enter_tapped" in blob):
            return e
    return None


def library_shuffle_commands(state):
    """resolved_rules_journal entries whose command is exactly LibraryShuffle.

    (Substring-matching 'shuffle' also hits StackEntryFinalize/StackRemoval
    entries whose JSON merely mentions the word; only the command type counts
    as an actual shuffle operation.)
    """
    out = []
    j = state.get("resolved_rules_journal", {}) or {}
    for e in j.get("entries", []) or []:
        cmd = e.get("command", {}) or {}
        if "LibraryShuffle" in cmd:
            out.append(e)
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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    for s in (ch.get("surfaces") or []):
        d = s.get("data") or {}
        for k in ("text", "label", "name", "value"):
            if d.get(k):
                return str(d[k])
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    return str(t)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_grazer_bf", "A3_etb_triggered",
            "A4_zenith_library", "A5_shuffle_exactly2", "A6_cleanup")}
    obs = {
        "stage": "setup",
        "zenith_cast": False,
        "x_chosen": None,
        "search_submitted": False,
        "grazer_oid": None,
        "trigger_seen": False,
        "trigger_entry": None,
        "optional_accepted": False,
        "land_put_oid": None,
        "mid_exported": False,
        "pre_exported": False,
        "post_exported": False,
        "pre_bf_forest": None,
        "pre_hand_forest": None,
        "done": False,
        "rejections": [],
        "finish_turn": None,
    }
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    game_code = p0.game_code
    say(f"game {game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"game_code": game_code})

    kept = {}
    submitted_interactions = set()
    shapes_logged = set()

    async def drain(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                rec = {"who": c.name, "type": t, "data": data}
                obs["rejections"].append(rec)
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", rec)

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json ({len(s)} bytes)")
            wire("export", {"tag": tag, "bytes": len(s)})
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            say(f"export {tag} FAILED: {e}")
            return False

    async def mulligan_branch(c, pid, tag, acts, state, wtype):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n in LANDS)
            mulls = kept.get(tag + "_mulls", 0)
            if tag == "P0":
                # Keep Zenith + >=2 Forests (one to play, one to keep for the
                # Grazer ETB so the land target prompt is not auto-resolved).
                want = ZENITH in hn and lands >= 2
            else:
                want = True
            if want or mulls >= 4:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps (mulls={mulls}, hand={[n for n in hn][:8]})")
            else:
                kept[tag + "_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{tag} mulligans #{mulls + 1}")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(tag + "_bot"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 0
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 0))
                if count > 0:
                    hids = hand_ids(state, pid)

                    def bkey(oid):
                        nm = lname(state, oid)
                        return 0 if nm in LANDS else (2 if nm in (ZENITH, GRAZER) else 1)
                    picks = sorted(hids, key=bkey)[:count]
                    kept[tag + "_bot"] = True
                    await submit_as_is(c, {"type": "SelectCards",
                                           "data": {"cards": [int(x) for x in picks]}})
                    say(f"{tag} bottoms {count}")
            return True
        return False

    async def discard_branch(c, pid, tag, state, wtype):
        """Answer a Discard* prompt only if pending for this pid. Discard
        Grazers first (they belong in the library), spare Zeniths next, and
        Forests last; never discard the last Zenith."""
        if not (wtype and "Discard" in wtype):
            return False
        wf = state.get("waiting_for") or {}
        data = wf.get("data") or {}
        named = data.get("player")
        if named is not None and named != pid:
            return False
        pending = data.get("pending") or []
        ours = any(p.get("player") == pid for p in pending)
        if pending and not ours:
            return False
        count = 0
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if "count" in ph:
                    count = int(ph["count"])
                break
        hids = hand_ids(state, pid)
        zen = [o for o in hids if lname(state, o) == ZENITH]

        def rank(oid):
            nm = lname(state, oid)
            if nm == GRAZER:
                return (0, nm)
            if nm == ZENITH:
                return (1 if len(zen) > 1 else 3, nm)
            return (2, nm)
        hids.sort(key=rank)
        if wtype == "DiscardToHandSize":
            n = max(0, len(hids) - 7)
        else:
            n = count if count else 2
        picks = hids[:n]
        if not picks:
            return False
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} discards {[lname(state, x) for x in picks]} ({wtype})")
        wire("discard", {"who": tag, "wtype": wtype,
                         "cards": [lname(state, x) for x in picks]})
        return True

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def action_codes(ch):
        out = []
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "action":
                out.append(((s.get("data") or {}).get("code")) or "")
        return out

    def accept_value(ch):
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "value":
                d = s.get("data") or {}
                if d.get("role") == "accept":
                    return str(d.get("value")).lower() == "true"
        return None

    def cand_ref(ch):
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                return str(d["reference"]), (d.get("zone") or "")
        return None, ""

    def cand_zone_names(ch, st_state):
        ref, _ = cand_ref(ch)
        if ref is not None:
            return lname(st_state, ref)
        return choice_text(ch).lower()

    async def scan_interactions(st, who):
        """Handle: Zenith X=0 (number prompt), the SearchLibrary prompt (pick
        the Grazer), the Grazer optional 'you may' (decideOptionalEffect
        accept), and the ETB land-from-hand target prompt (schema, land
        candidates in hand). Priority menus are never touched here."""
        nonlocal obs
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        c = p0 if who == "P0" else p1
        state = st.get("state") or {}
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            codes = set()
            for ch in chs:
                codes.update(action_codes(ch))
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            key = (who, rtype, spec_type, blob[:80], tuple(sorted(codes))[:3])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} spec={spec_type} n={len(chs)} "
                    f"codes={sorted(codes)[:4]} choices=[{blob[:220]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "spec_type": spec_type,
                                           "interaction": opp})
            if iid in submitted_interactions:
                continue
            if who != "P0":
                continue
            # --- Zenith X choice (schema number); Grazer is MV 1 ---
            if (rtype == "schema" and spec_type == "number"
                    and obs["x_chosen"] is None and obs["zenith_cast"]):
                sub = {"interactionId": iid,
                       "response": {"type": "number", "data": {"value": 1}}}
                say("[P0] Zenith X-choice -> X=1")
                wire("x_choice", {"submission": sub})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["x_chosen"] = 1
                acted = True
                continue
            # --- priority menus: never touch ---
            if codes - {"decideOptionalEffect"}:
                continue
            # --- optional "you may" (Grazer ETB): accept ---
            if "decideOptionalEffect" in codes:
                pick = next((ch for ch in chs if accept_value(ch) is True), None)
                if pick is None:
                    notes.append(f"optional prompt without accept choice "
                                 f"(iid={iid}); skipping")
                    wire("optional_no_accept", {"iid": iid})
                    continue
                sub = {"interactionId": iid, "response":
                       {"type": "choose", "data": {"choiceId": pick["id"]}}}
                say("[P0] Grazer ETB optional 'you may' -> ACCEPT")
                wire("optional_accept", {"submission": sub})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["optional_accepted"] = True
                acted = True
                continue
            # --- library search prompt (pick the Grazer) ---
            grazer_ch = next(
                (ch for ch in chs
                 if "arboreal grazer" in choice_text(ch).lower()
                 or "arboreal grazer" in cand_zone_names(ch, state)), None)
            if grazer_ch is not None and not obs["search_submitted"]:
                ids = [grazer_ch["id"]]
                sub_type = spec_type or "sequence"
                if rtype == "exactChoices":
                    sub = {"interactionId": iid, "response":
                           {"type": "choose", "data": {"choiceId": ids[0]}}}
                else:
                    sub = {"interactionId": iid, "response":
                           {"type": sub_type, "data": {"choiceIds": ids}}}
                say("[P0] Zenith search -> Arboreal Grazer")
                wire("search_choice", {"submission": sub})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["search_submitted"] = True
                acted = True
                continue
            # --- ETB land-from-hand target prompt (schema, land candidates) ---
            if rtype == "schema" and spec_type in ("sequence", "select"):
                hand_land = []
                for ch in chs:
                    ref, zone = cand_ref(ch)
                    nm = cand_zone_names(ch, state) if ref else ""
                    if ref is not None and str(zone).lower() == "hand" \
                            and nm == FOREST:
                        hand_land.append(ch)
                if hand_land:
                    pick = hand_land[0]
                    sub = {"interactionId": iid, "response":
                           {"type": spec_type, "data": {"choiceIds": [pick["id"]]}}}
                    say(f"[P0] Grazer ETB land target -> Forest "
                        f"(ref={cand_ref(pick)[0]})")
                    wire("etb_land_target", {"submission": sub})
                    await c.send_interaction(sub)
                    submitted_interactions.add(iid)
                    obs["land_put_oid"] = cand_ref(pick)[0]
                    acted = True
                    continue
        return acted

    async def p0_tick(st):
        state = st.get("state") or {}
        acts = merged_actions(st)
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_branch(p0, 0, "P0", acts, state, wtype):
            return
        if await discard_branch(p0, 0, "P0", state, wtype):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # pre export FIRST: Zenith cast with X chosen, before the search
        # resolves and the Grazer hits the battlefield. Must run before
        # scan_interactions, which would otherwise answer the search prompt in
        # the same tick X resolves.
        grazers_early = bf_ids(state, 0, GRAZER)
        if (obs["zenith_cast"] and obs["x_chosen"] is not None
                and not obs["pre_exported"] and grazers_early == []):
            if await export("pre"):
                obs["pre_exported"] = True
                obs["stage"] = "pre"
                pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
                obs["pre_bf_forest"] = len(bf_ids(pre_st, 0, FOREST))
                obs["pre_hand_forest"] = hand_names(pre_st, 0).count(FOREST)
                say(f"pre: bf_forest={obs['pre_bf_forest']} "
                    f"hand_forest={obs['pre_hand_forest']}")
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "blocks" in sub["data"]:
                    sub["data"]["blocks"] = []
                await submit_as_is(p0, sub)
                say("P0 declares no attackers/blockers")
                return
        if await scan_interactions(st, "P0"):
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        turn = state.get("turn_number")
        phase = state.get("phase")
        grazers = bf_ids(state, 0, GRAZER)
        if grazers and obs["grazer_oid"] is None:
            obs["grazer_oid"] = grazers[0]
            obs["stage"] = "grazer_on_bf"
            say(f"Grazer on battlefield: oid {grazers[0]}")
            wire("grazer_on_bf", {"oid": grazers[0]})
        hit = grazer_trigger_on_stack(state, obs["grazer_oid"])
        if hit and not obs["trigger_seen"]:
            obs["trigger_seen"] = True
            obs["trigger_entry"] = json.dumps(hit, default=str)[:1500]
            say("GRAZER ETB TRIGGER observed on stack")
            wire("grazer_trigger_on_stack", hit)
            await export("mid_trigger")
            obs["mid_exported"] = True
        # pre export fallback (also runs earlier, before interactions); kept
        # here for the case where the spell hits the stack while grazers
        # were already counted empty above.
        if (zenith_on_stack(state) and not obs["pre_exported"]
                and grazers == []):
            if await export("pre"):
                obs["pre_exported"] = True
                obs["stage"] = "pre"
                pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
                obs["pre_bf_forest"] = len(bf_ids(pre_st, 0, FOREST))
                obs["pre_hand_forest"] = hand_names(pre_st, 0).count(FOREST)
                say(f"pre: bf_forest={obs['pre_bf_forest']} "
                    f"hand_forest={obs['pre_hand_forest']}")
        # post condition: Zenith resolved, Grazer on BF, stack empty
        if (obs["zenith_cast"] and grazers and not zenith_on_stack(state)
                and len(state.get("stack", []) or []) == 0
                and not obs["post_exported"]):
            if await export("post"):
                obs["post_exported"] = True
                obs["done"] = True
                obs["finish_turn"] = turn
                say(f"post: turn={turn} phase={phase}; resolution complete")
            return
        # main-phase play: one land per turn, then cast the Zenith with X=1
        # only once 2 untapped Forests can actually pay {1}{G} (the engine's
        # ChooseXValue prompt rejects an X the mana pool cannot cover).
        if phase in ("PreCombatMain", "PostCombatMain"):
            for oid in hand_ids(state, 0):
                if lname(state, oid) == FOREST:
                    la = find_action(acts, "PlayLand")
                    if la:
                        await submit_as_is(p0, la)
                        say(f"P0 plays Forest t{turn}")
                        wire("land", {"who": "P0", "turn": turn})
                        return
        if not obs["zenith_cast"] and len(untapped_bf_ids(state, 0, FOREST)) >= 2:
            for oid in hand_ids(state, 0):
                if lname(state, oid) == ZENITH:
                    cs = next((a for a in acts if a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("object_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("card_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("source_id")) == str(oid)), None)
                    if cs:
                        await submit_as_is(p0, cs)
                        obs["zenith_cast"] = True
                        obs["stage"] = "zenith_cast"
                        say(f"P0 casts Green Sun's Zenith t{turn}")
                        wire("cast", {"card": ZENITH, "turn": turn,
                                      "action_data": cs.get("data")})
                        return
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p0, pa)
            return

    async def p1_tick(st):
        state = st.get("state") or {}
        acts = merged_actions(st)
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_branch(p1, 1, "P1", acts, state, wtype):
            return
        if await discard_branch(p1, 1, "P1", state, wtype):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "blocks" in sub["data"]:
                    sub["data"]["blocks"] = []
                await submit_as_is(p1, sub)
                return
        if await scan_interactions(st, "P1"):
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        turn = state.get("turn_number")
        phase = state.get("phase")
        if phase in ("PreCombatMain", "PostCombatMain"):
            la = find_action(acts, "PlayLand")
            if la:
                await submit_as_is(p1, la)
                wire("land", {"who": "P1", "turn": turn})
                return
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p1, pa)

    # ---- main loop ----
    last_sig = None
    stall_watch = time.time()
    while time.time() - t_start < 900 and not obs["done"]:
        await asyncio.sleep(0.15)
        await drain(p0)
        await drain(p1)
        if p0.latest:
            await p0_tick(p0.latest)
        if p1.latest:
            await p1_tick(p1.latest)
        st = p0.latest or {}
        state = st.get("state") or {}
        sig = (state.get("turn_number"), state.get("phase"),
               (state.get("waiting_for") or {}).get("type"))
        if sig != last_sig:
            last_sig = sig
            stall_watch = time.time()
            say(f"[tick] turn={sig[0]} phase={sig[1]} wait={sig[2]} "
                f"stage={obs['stage']}")
        if obs["done"]:
            break
        if time.time() - stall_watch > 240:
            notes.append(f"stall watchdog: no state progress for 240s at {sig}")
            say("STALL WATCHDOG fired")
            break
        if (state.get("turn_number") or 0) > 15 and not obs["zenith_cast"]:
            notes.append("turn cap reached without the Zenith cast")
            say("turn cap reached without cast")
            break

    # ---- assertions ----
    def load_env(tag):
        try:
            return json.loads(open(f"{EVDIR}/{tag}.json").read())["state"]
        except Exception:
            return None

    pre_st = load_env("pre")
    post_st = load_env("post")
    if obs["zenith_cast"] and obs["x_chosen"] == 1 and obs["search_submitted"]:
        ass["A1_setup_ok"] = "passed"
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1: cast={obs['zenith_cast']} x={obs['x_chosen']} "
                     f"search={obs['search_submitted']}")
    if post_st is not None:
        grazers = bf_ids(post_st, 0, GRAZER)
        if grazers:
            ass["A2_grazer_bf"] = "passed"
            obs["grazer_oid"] = grazers[0]
        else:
            ass["A2_grazer_bf"] = "failed"
            notes.append("A2: no Arboreal Grazer on P0 battlefield in post.json")
        # A3: trigger seen + accepted + land entered tapped from hand.
        # Strongest check: the exact chosen land object is on the BF tapped.
        post_bf_forest = len(bf_ids(post_st, 0, FOREST))
        post_hand_forest = hand_names(post_st, 0).count(FOREST)
        land_delta = post_bf_forest - (obs["pre_bf_forest"] or 0)
        tapped_new = []
        for oid in bf_ids(post_st, 0, FOREST):
            o = get_obj(post_st, oid)
            if o.get("tapped"):
                tapped_new.append(oid)
        chosen_ok = False
        if obs["land_put_oid"] is not None:
            co = get_obj(post_st, obs["land_put_oid"])
            chosen_ok = (co.get("zone") == "Battlefield" and co.get("tapped"))
        if obs["trigger_seen"] and obs["optional_accepted"] and land_delta >= 1 \
                and chosen_ok \
                and post_hand_forest <= (obs["pre_hand_forest"] or 0):
            ass["A3_etb_triggered"] = "passed"
        else:
            ass["A3_etb_triggered"] = "failed"
            notes.append(f"A3: trigger_seen={obs['trigger_seen']} "
                         f"optional_accepted={obs['optional_accepted']} "
                         f"bf_forest {obs['pre_bf_forest']}->{post_bf_forest} "
                         f"(tapped on BF: {len(tapped_new)}) "
                         f"chosen land {obs['land_put_oid']} bf+tapped={chosen_ok} "
                         f"hand_forest {obs['pre_hand_forest']}->{post_hand_forest}")
        # A4: Zenith in P0 library, not graveyard
        post_lib = lib_names(post_st, 0)
        post_gy = gy_names(post_st, 0)
        zen_in_lib = post_lib.count(ZENITH)
        zen_in_gy = post_gy.count(ZENITH)
        if zen_in_lib >= 1 and zen_in_gy == 0:
            ass["A4_zenith_library"] = "passed"
        else:
            ass["A4_zenith_library"] = "failed"
            notes.append(f"A4: Zenith in library={zen_in_lib}, in graveyard={zen_in_gy}")
        # A5: exactly two LibraryShuffle commands between pre and post
        # (the two Oracle shuffle instructions).
        if pre_st is not None:
            pre_sh = library_shuffle_commands(pre_st)
            post_sh = library_shuffle_commands(post_st)
            new_shuffles = len(post_sh) - len(pre_sh)
            obs["shuffles_pre"] = len(pre_sh)
            obs["shuffles_post"] = len(post_sh)
            obs["shuffle_word_ranges"] = [
                (e["command"]["LibraryShuffle"].get("pre_word_pos"),
                 e["command"]["LibraryShuffle"].get("post_word_pos"))
                for e in post_sh[len(pre_sh):]]
            if new_shuffles == 2:
                ass["A5_shuffle_exactly2"] = "passed"
            else:
                ass["A5_shuffle_exactly2"] = "failed"
                notes.append(f"A5: {new_shuffles} LibraryShuffle commands during "
                             f"the Zenith resolution (pre={len(pre_sh)}, "
                             f"post={len(post_sh)}, rng word ranges "
                             f"{obs['shuffle_word_ranges']}); expected 2")
        else:
            ass["A5_shuffle_exactly2"] = "not-run"
            notes.append("A5: not-run (pre.json missing)")
        # A6: stack empty, game proceeding
        if len(post_st.get("stack", []) or []) == 0:
            ass["A6_cleanup"] = "passed"
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"A6: stack not empty in post.json "
                         f"({len(post_st.get('stack', []))} entries)")
    else:
        for k in ("A2_grazer_bf", "A3_etb_triggered", "A4_zenith_library",
                  "A6_cleanup"):
            ass[k] = "not-run"
        notes.append("A2/A3/A4/A6: not-run (post.json missing)")
        ass["A5_shuffle_exactly2"] = "not-run"

    # ---- verdict ----
    if ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
        notes.append("verdict=blocked: the Zenith cast sequence never completed; "
                     "no trustworthy result on the reported outcome")
    elif (ass["A2_grazer_bf"] == "passed"
          and ass["A3_etb_triggered"] == "failed"):
        verdict = "reproduced"
        notes.append("verdict=reproduced: the searched Grazer reached the "
                     "battlefield but its enters trigger never fired/resolved")
    elif (ass["A4_zenith_library"] == "failed"
          or ass["A5_shuffle_exactly2"] == "failed"):
        verdict = "reproduced"
        notes.append("verdict=reproduced (secondary observation from the "
                     "report): the ETB itself fired and resolved, but the "
                     "Zenith resolution deviates from the report's expected "
                     "behavior (wrong destination and/or extra shuffles)")
    elif (ass["A2_grazer_bf"] == "passed" and ass["A3_etb_triggered"] == "passed"
          and ass["A4_zenith_library"] == "passed"
          and ass["A5_shuffle_exactly2"] == "passed"
          and ass["A6_cleanup"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete assertion set; no trustworthy "
                     "result")

    say("assertions: " + json.dumps(ass))
    say("verdict: " + verdict)
    for n_ in notes:
        say("note: " + n_)

    # ---- parse evidence ----
    try:
        import glob
        card_data_path = (f"{BACKFILL}/server/releases/v0.78.0/data/card-data.json")
        card_data = json.load(open(card_data_path))
        parse_evidence = {
            "zenith_key": "green sun's zenith",
            "zenith_oracle": card_data["green sun's zenith"]["oracle_text"],
            "zenith_abilities": card_data["green sun's zenith"]["abilities"],
            "grazer_oracle": card_data["arboreal grazer"]["oracle_text"],
            "grazer_triggers": card_data["arboreal grazer"]["triggers"],
            "observation": "The search-following ChangeZone carries target "
                           "Any (not the found card); Grazer's ChangesZone "
                           "trigger is typed faithfully with SelfRef.",
        }
        with open(f"{EVDIR}/parse_evidence.json", "w") as f:
            json.dump(parse_evidence, f, indent=1, default=str)
        say("wrote parse_evidence.json")
    except Exception as e:
        notes.append(f"parse evidence capture failed: {e}")
        say(f"parse evidence failed: {e}")

    # ---- run.json ----
    duration_s = round(time.time() - t_start, 1)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "server": SERVER_IDENTITY,
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "games": [{
            "tag": "A",
            "desc": "Green Sun's Zenith X=0 tutors Arboreal Grazer; the Grazer "
                    "ETB ('you may put a land card from your hand onto the "
                    "battlefield tapped') is accepted and a Forest is put "
                    "tapped; Zenith destination and shuffle counts asserted",
            "assertions": ass,
            "observations": {
                "zenith_cast": obs["zenith_cast"],
                "x_chosen": obs["x_chosen"],
                "search_submitted": obs["search_submitted"],
                "grazer_oid": obs["grazer_oid"],
                "trigger_seen": obs["trigger_seen"],
                "trigger_entry": obs["trigger_entry"],
                "optional_accepted": obs["optional_accepted"],
                "land_put_oid": obs["land_put_oid"],
                "pre_bf_forest": obs["pre_bf_forest"],
                "pre_hand_forest": obs["pre_hand_forest"],
                "shuffles_pre": obs.get("shuffles_pre"),
                "shuffles_post": obs.get("shuffles_post"),
                "rejections": obs["rejections"],
            },
            "notes": notes,
        }],
        "verdict": verdict,
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
        "duration_s": duration_s,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # ---- server excerpts ----
    try:
        slog = f"{BACKFILL}/runs/{RUN_ID}/server.log"
        excerpts = []
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    if game_code in line:
                        excerpts.append(line.rstrip())
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines for {game_code}")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    print(f"VERDICT:{verdict}")


asyncio.run(main())
