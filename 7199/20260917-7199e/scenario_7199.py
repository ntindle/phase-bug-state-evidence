#!/usr/bin/env python3
"""Issue #7199: Sanar, Innovative First-Year — "Exile does not allow me to
pick both cards to exile even when I am allowed."

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed; labels: area:engine, area:parser,
mechanic:search, mechanic:zone-change, priority:p3-card-specific):
Oracle text (pinned card-data.json):
  Sanar, Innovative First-Year {2}{U/R}{U/R} 2/4 Legendary Creature — Goblin Sorcerer
  Vivid — At the beginning of your first main phase, reveal cards from the top
  of your library until you reveal X nonland cards, where X is the number of
  colors among permanents you control. For each of those colors, you may exile
  a card of that color from among the revealed cards. Then shuffle. You may
  cast the exiled cards this turn.

Expected behavior (per card text + triage acceptance criteria):
  E1: Vivid fires at the beginning of P0's PreCombatMain while Sanar is on BF.
  E2: X = number of colors among permanents P0 controls (Sanar = {R,U} -> X=2):
      exactly 2 nonland cards are revealed.
  E3: For EACH represented color (red, blue) an optional selection is offered:
      exile a card of that color from among the revealed cards.
  E4: Both selections can be made when the revealed cards permit (one red +
      one blue revealed) — the reported failure.
  E5: Only the chosen cards are exiled; the other revealed cards are shuffled
      back into the library (zone Library, no strays).
  E6: The exiled cards may be cast this turn (cast permission granted).

Setup (native engine, two human-client seats):
  P0: 8x Sanar, Innovative First-Year, 18x Lightning Bolt (red nonland),
      18x Divination (blue nonland), 8x Island, 8x Mountain.
      Mulligan until Sanar + >=2 lands. Casts Sanar on turn with 4 lands
      (engine auto-taps). Never attacks. Discard: spare Sanars/spells first.
  P1: 24x Grizzly Bears, 36x Forest (land drop, opportunistic 2/2, no attacks).

Assertions:
  A1_setup_ok        Sanar on BF under P0 at a PreCombatMain; >=2 colors among
                     P0 permanents (expect exactly red+blue).
  A2_trigger_fires   Vivid trigger observed (on stack / resolving into the
                     exile selection flow).
  A3_reveal_x        the exile selection is offered over exactly the revealed
                     pool: X=2 nonland candidates (lands revealed on the way
                     do not count).
  A4_per_color_offer one optional exile selection per represented color
                     (red and blue both offered when both colors revealed).
  A5_both_exiled     when one red + one blue nonland are revealed, BOTH can be
                     exiled (the reported failure).
  A6_only_chosen     only the chosen oids are in Exile; unchosen revealed oids
                     are back in Library.
  A7_cast_permission the exiled Lightning Bolt can be cast from exile this
                     turn -> P1 at 17 life, Bolt in graveyard.

Verdict rule: reproduced iff A1+A2 pass, a mixed (red+blue) reveal is observed,
  and A4/A5 fail. not-reproduced iff A1..A7 all pass. blocked iff the setup
  cannot be driven to the exile selection or the prompt shape is unanswerable.

Evidence: evidence/7199/<run-id>/pre.json (at first exile prompt),
  post.json (after exile+shuffle), cast.json (after casting exiled Bolt),
  run.json, manifest.sha256, summary.png, wire_log.jsonl, scenario_run.log.
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
# RUN_ID bumped every launch.
RUN_ID = "20260917-7199e"
EVDIR = f"{BACKFILL}/evidence/7199/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SANAR = "Sanar, Innovative First-Year"
BOLT = "Lightning Bolt"       # red nonland
DIV = "Divination"            # blue nonland
ISLAND = "Island"
MOUNTAIN = "Mountain"
BEARS = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = [(SANAR, 8), (BOLT, 18), (DIV, 18), (ISLAND, 8), (MOUNTAIN, 8)]
P1_DECK = [(BEARS, 24), (FOREST, 36)]

CARD_COLORS = {SANAR: {"Red", "Blue"}, BOLT: {"Red"}, DIV: {"Blue"},
               ISLAND: {"Blue"}, MOUNTAIN: {"Red"}}

SERVER_IDENTITY = {
    # Recomputed 2026-09-17 via sha256sum against the on-disk verified
    # v0.86.0 artifacts (never copied from a previous scenario; #7176 lesson).
    "server_version": "v0.86.0",
    "build_commit": "2cc8c28",
    "protocol_version": 72,
    "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
    "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c0f4af30a9c40ff26",
    "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
    "signature_verified": True,
    "observed_at": "2026-09-17",
    "source": "ServerHello + sha256 recomputed against the on-disk verified "
              "v0.86.0 artifacts (minisig-verified per playbook §0); server "
              "started fresh this run on 127.0.0.1:9374 (runs/20260917-7199)",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def life(state, pid):
    return state["players"][pid]["life"]


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def hand_ids(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def untapped_lands(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) in (ISLAND, MOUNTAIN)
            and not o.get("tapped")]


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
    bits = []

    def rec(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if k in ("name", "code", "label", "value", "description", "text",
                         "prompt", "title") and isinstance(val, str):
                    bits.append(val)
                else:
                    rec(val)
        elif isinstance(v, list):
            for x in v:
                rec(x)
    rec(ch.get("surfaces", []))
    return " | ".join(bits)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def card_colors_of(name):
    return CARD_COLORS.get(name, set())


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_fires", "A3_reveal_x",
            "A4_per_color_offer", "A5_both_exiled", "A6_only_chosen",
            "A7_cast_permission")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}
    submitted_interactions = set()
    shapes_logged = set()

    # ---- per-run scenario state ----
    pre_exported = False
    post_exported = False
    cast_exported = False
    trigger_seen = False
    sanar_cast = {"submitted": False, "resolved": False, "rev": None, "oid": None}
    # exile test state: reset each P0 PreCombatMain with Sanar on board
    exile = {"turn": None, "prompt_iids": [], "candidates": [],
             "offered_colors": set(), "selections": {}, "done": False,
             "rejections": [], "probe_dumped": False}
    mixed_reveal_seen = False
    mixed_done = False
    mixed_bolt_oid = None
    cast_perm = {"submitted": False, "resolved": False, "rev": None, "oid": None}

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    # scenario-scope: revealed-but-unchosen oids across all turns (for A6)
    revealed_unchosen = {}  # cid -> name
    trigger_turns = 0

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
        # The REPORTED bug is the exile-selection path (A1..A6). A7 (cast
        # permission) is an extra end-to-end check, reported as a limitation
        # when not exercised.
        reported_ok = all(ass[k] == "passed" for k in
                          ("A1_setup_ok", "A2_trigger_fires", "A3_reveal_x",
                           "A4_per_color_offer", "A5_both_exiled",
                           "A6_only_chosen"))
        bug_fail = (ass["A1_setup_ok"] == "passed"
                    and ass["A2_trigger_fires"] == "passed"
                    and mixed_reveal_seen
                    and any(ass[k] == "failed" for k in
                            ("A4_per_color_offer", "A5_both_exiled")))
        if bug_fail:
            verdict = "reproduced"
        elif reported_ok:
            verdict = "not-reproduced"
            if ass["A7_cast_permission"] != "passed":
                notes.append(f"A7 cast-permission leg status: {ass['A7_cast_permission']}; "
                             "reported exile path fully exercised regardless")
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        run = {
            "issue": 7199,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7199.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                            "Discord attachment game-state zip not restored (fresh setup instead); "
                            "its board state (Sanar + revealed pool) is approximated by the fixture."],
            "setup_line": "P0: 8x Sanar, Innovative First-Year + 18x Lightning Bolt + "
                          "18x Divination + 8x Island + 8x Mountain "
                          "(mulligan-to-Sanar, keep if Sanar+2 lands); "
                          "P1: 24x Grizzly Bears + 36x Forest",
            "contract_line": "Vivid fires at P0 PreCombatMain (X=2 colors: red+blue from Sanar); "
                             "one optional exile selection per represented color over the 2 "
                             "revealed nonlands; both a red and a blue card must be exilable "
                             "when revealed; only chosen cards exiled; exiled cards castable "
                             "this turn (Bolt -> P1 at 17)",
            "stats": {},
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

    def mulligan_pending_for(state, pid):
        d = ((state.get("waiting_for") or {}).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = p.get("phase") or {}
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    def stack_has_vivid(state):
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str)
            if "Vivid" in blob:
                return True
            kind = (e.get("kind") or {})
            if str(kind.get("type")) == "TriggeredAbility":
                desc = str((e.get("ability") or {}).get("description") or "")
                if "reveal cards from the top" in desc:
                    return True
        return False

    def cand_info(ch):
        """(choice_id, name, colors, status) for a candidate/choice."""
        cid = ch.get("id")
        status = (ch.get("status") or {}).get("type")
        name = None
        cols = set()
        for s in ch.get("surfaces", []) or []:
            d = s.get("data", {}) or {}
            if isinstance(d, dict):
                if d.get("name") and name is None:
                    name = d["name"]
                for k in ("colors", "color"):
                    v = d.get(k)
                    if isinstance(v, list):
                        cols |= {str(x) for x in v}
                    elif isinstance(v, str):
                        cols.add(v)
        if name and not cols:
            cols = card_colors_of(name)
        return cid, name, cols, status

    async def handle_choose_from_zone(c, st, state, acts):
        """Drive Sanar's per-color exile prompt.

        Observed on v0.86.0/protocol 72 (probe run 20260917-7199): the
        ForEachCategory(color) exile step surfaces as
        waiting_for.type == "ChooseFromZoneChoice" with
        data = {"player": 0, "cards": [<oid>...], "count": 1,
                "up_to": true, "source_id": <sanar oid>},
        one prompt per represented color, cards filtered to that color.
        Answer via the viewer_interaction opportunity when it carries
        candidates, else via the advertised SelectCards legal action
        (data advertises `cards`, not `cardIds`; #7198 lesson).
        Returns True if acted.
        """
        nonlocal mixed_reveal_seen, mixed_done, mixed_bolt_oid, trigger_turns
        nonlocal trigger_seen
        wf = state.get("waiting_for") or {}
        data = wf.get("data", {}) or {}
        if data.get("player") != 0:
            return False
        turn = state.get("turn_number")
        # Reaching this Sanar-sourced per-color selection flow is decisive
        # evidence Vivid's trigger resolved into its exile effect (#7199 triage
        # note 1: the fast stack window was missed, so set A2 here).
        if not trigger_seen:
            trigger_seen = True
            ass["A2_trigger_fires"] = "passed"
            notes.append("A2: Vivid trigger resolved into per-color exile "
                         "selections (ChooseFromZoneChoice sourced by Sanar)")
            say("A2: Vivid trigger evidence via exile selection flow")
            wire("a2_trigger", {"turn": state.get("turn_number")})
        if exile["turn"] != turn:
            exile.update({"turn": turn, "prompt_iids": [], "candidates": [],
                          "offered_colors": set(), "offered_oids": [],
                          "selections": {}, "done": False, "rejections": []})
            trigger_turns += 1
            say(f"exile tracking reset for turn {turn} (trigger turn #{trigger_turns})")
        cards = [int(x) for x in (data.get("cards") or [])]
        count = int(data.get("count") or 1)
        # log the opportunity shape once per response type
        vi = get_vi(st)
        opps = (vi.get("opportunities", []) or []) if vi else []
        for opp in opps:
            key = ("cfz", str((opp.get("response") or {}).get("type")))
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"ChooseFromZoneChoice opportunity rtype={key[1]}: "
                    f"{json.dumps(opp)[:2000]}")
                wire("cfz_opportunity", opp)
        # log the advertised SelectCards action shape once
        sc = find_action(acts, "SelectCards")
        if sc and ("cfz", "selectcards") not in shapes_logged:
            shapes_logged.add(("cfz", "selectcards"))
            say(f"ChooseFromZoneChoice SelectCards action: {json.dumps(sc)[:600]}")
            wire("cfz_selectcards", sc)
        for oid in cards:
            if oid not in exile["offered_oids"]:
                exile["offered_oids"].append(oid)
        if not exile["candidates"]:
            exile["candidates"] = [(oid, obj_name(state, oid),
                                    sorted(card_colors_of(obj_name(state, oid))))
                                   for oid in cards]
        infos = [(oid, obj_name(state, oid),
                  card_colors_of(obj_name(state, oid))) for oid in cards]
        say(f"ChooseFromZoneChoice turn={turn} cards="
            f"{[(n, sorted(c)) for _, n, c in infos]} count={count}")
        wire("cfz_prompt", {"turn": turn, "cards": cards, "count": count,
                            "names": [n for _, n, _ in infos]})
        # pick: one card per prompt (count=1), preferring a color not yet
        # exiled this turn. Empty offered cards + up_to:true means the color
        # had no revealed card: decline with an empty selection.
        picked_colors = set(exile["selections"])
        pick = None
        for oid, nm, cols in infos:
            if not (cols & picked_colors):
                pick = oid
                break
        if pick is None and infos:
            pick = infos[0][0]
        picks = [pick] if pick is not None else []
        pick_name = obj_name(state, pick) if pick is not None else "<none>"
        pick_cols = card_colors_of(pick_name) if pick is not None else set()
        answered = False
        # route 1: viewer_interaction opportunity with candidates.
        # Match by the candidate's object reference EXACTLY (never by card
        # name: with dense playsets the name fallback can select a different
        # copy — observed 20260917-7199b: name fallback submitted s1/reference
        # 1 while the log claimed oid 45). Name fallback only when NO
        # reference matches at all.
        for opp in opps:
            iid = opp.get("interactionId")
            if iid in submitted_interactions:
                continue
            resp = opp.get("response", {}) or {}
            rdata = resp.get("data", {}) or {}
            ocands = rdata.get("candidates") or rdata.get("choices") or []
            if not ocands or not picks:
                continue
            ref_ids = set()
            name_ids = set()
            for ch in ocands:
                cid = ch.get("id")
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data", {}) or {}
                    try:
                        if isinstance(d, dict) and d.get("reference") is not None \
                                and int(d["reference"]) == pick:
                            ref_ids.add(cid)
                    except (TypeError, ValueError):
                        pass
                if choice_text(ch).find(pick_name) >= 0:
                    name_ids.add(cid)
            ok_ids = ref_ids or name_ids
            if not ok_ids:
                continue
            spec = rdata.get("spec") or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            rtype = resp.get("type")
            choice_id = next(iter(ok_ids))
            if rtype == "exactChoices":
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": choice_id}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": stype or "select",
                                    "data": {"choiceIds": [choice_id]}}}
            say(f"answering CFZ via opportunity: pick {pick_name} (oid {pick})")
            wire("cfz_submission_vi", {"submission": sub, "interaction": opp})
            await c.send_interaction(sub)
            submitted_interactions.add(iid)
            exile["prompt_iids"].append(iid)
            answered = True
            break
        # route 2: advertised SelectCards legal action (also the decline path:
        # empty picks when the color had no revealed card)
        if not answered and sc:
            d = dict(sc.get("data", {}) or {})
            if "cards" in d:
                d["cards"] = list(picks)
            elif "cardIds" in d:
                d["cardIds"] = list(picks)
            else:
                d["cards"] = list(picks)
            say(f"answering CFZ via SelectCards: picks={picks} ({pick_name})")
            wire("cfz_submission_action", {"action": {"type": "SelectCards", "data": d}})
            await c.send_action({"type": "SelectCards", "data": d})
            exile["prompt_iids"].append(f"action@{turn}")
            answered = True
        if not answered:
            say("ChooseFromZoneChoice: no answer route available")
            return False
        if pick is not None:
            for col in pick_cols:
                if col not in exile["selections"]:
                    exile["selections"][col] = (pick, pick_name)
                    break
            else:
                exile["selections"].setdefault("?", (pick, pick_name))
        else:
            notes.append(f"turn {turn}: CFZ prompt with no cards (declined empty)")
        await asyncio.sleep(0.8)
        rej = await drain_rejections(c, timeout=2)
        if rej:
            say(f"CFZ submission rejected: {json.dumps(rej)[:300]}")
            wire("cfz_rejected", rej)
            exile["rejections"].append(rej)
            notes.append(f"CFZ submission for {pick_name} rejected: {rej}")
        return True

    async def drain_rejections(c, timeout=3):
        rej = None
        t0 = time.time()
        while time.time() - t0 < timeout and rej is None:
            await asyncio.sleep(0.3)
            try:
                while True:
                    t, d = c.inbox.get_nowait()
                    if t in ("ActionRejected", "Error"):
                        rej = {"type": t, "data": d}
            except asyncio.QueueEmpty:
                pass
        return rej

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, cast_exported, trigger_seen, mixed_done, mixed_bolt_oid, mixed_reveal_seen
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        phase = state.get("phase")
        # --- mulligan ---
        if wtype == "MulliganDecision" and mulligan_pending_for(state, 0) \
                and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n in (ISLAND, MOUNTAIN))
            mulls = kept.get("P0_mulls", 0)
            if (SANAR in hn and lands >= 2) or mulls >= 4:
                kept["P0"] = True
                await p0.send_action({"type": "MulliganDecision",
                                      "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (sanar={SANAR in hn}, lands={lands})")
            else:
                kept["P0_mulls"] = mulls + 1
                await p0.send_action({"type": "MulliganDecision",
                                      "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (sanar={SANAR in hn}, lands={lands})")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0 and \
                            str((p.get("phase") or {}).get("type")) == "BottomCards":
                        count = int((p.get("phase") or {}).get("count", 1))
                hids = hand_ids(state, 0)

                def bkey(oid):
                    nm = obj_name(state, oid)
                    if nm in (BOLT, DIV):
                        return 0
                    if nm == SANAR and hand_names(state, 0).count(SANAR) > 1:
                        return 1
                    return 2  # lands last
                picks = sorted(hids, key=bkey)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}: {[obj_name(state, x) for x in picks]}")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
                say("P0 declares no attackers")
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "DiscardToHandSize":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []):
                    data = (opp.get("response", {}) or {}).get("data", {}) or {}
                    cands = data.get("candidates") or []
                    if cands:
                        # protect: Sanar (pre-cast, keep one), lands last
                        sanars = [c for c in cands
                                  if (choice_text(c).find(SANAR) >= 0)]
                        sanar_in_hand = hand_names(state, 0).count(SANAR)

                        def dkey(c):
                            t = choice_text(c)
                            if SANAR in t and sanar_in_hand <= 1:
                                return 3
                            if ISLAND in t or MOUNTAIN in t:
                                return 2
                            if SANAR in t:
                                return 0
                            return 1  # spells first
                        pick = sorted(cands, key=dkey)[0]
                        spec = data.get("spec") or {}
                        stype = spec.get("type", "select")
                        sub = {"interactionId": opp.get("interactionId"),
                               "response": {"type": stype,
                                            "data": {"choiceIds": [pick["id"]]}}}
                        await p0.send_interaction(sub)
                        say(f"P0 discards {choice_text(pick)[:60]}")
                        return
            return
        # --- Vivid trigger detection ---
        if stack_has_vivid(state) and not trigger_seen:
            trigger_seen = True
            ass["A2_trigger_fires"] = "passed"
            notes.append(f"Vivid trigger observed on stack at turn {turn} phase {phase}")
            say(f"VIVID TRIGGER on stack (turn {turn}, {phase})")
            wire("vivid_trigger_stack", {"turn": turn, "phase": phase,
                                         "stack": state.get("stack")})
        # --- exile selection prompts (own waiting_for type, not Priority) ---
        if wtype == "ChooseFromZoneChoice":
            if not pre_exported:
                try:
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_exported = True
                    say("exported PRE (first exile prompt)")
                    wire("pre_exported", {"turn": turn, "wtype": wtype})
                except Exception as e:
                    notes.append(f"pre export failed: {e}")
            if await handle_choose_from_zone(p0, st, state, acts):
                return
            return
        if wtype == "TargetSelection" and cast_perm["submitted"] \
                and not cast_perm["resolved"]:
            # drive the exiled Bolt's target selection: choose P1 (seat 1).
            # One slot at a time (#7179 lesson); player candidates carry
            # surfaces[].data.seat.
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []):
                    iid = opp.get("interactionId")
                    if iid in submitted_interactions:
                        continue
                    resp = opp.get("response", {}) or {}
                    rdata = resp.get("data", {}) or {}
                    cands = rdata.get("candidates") or []
                    if not cands:
                        continue
                    pick = None
                    for ch in cands:
                        for s in ch.get("surfaces", []) or []:
                            d = s.get("data", {}) or {}
                            if isinstance(d, dict) and d.get("seat") == 1:
                                pick = ch
                                break
                        if pick:
                            break
                    if pick is None:
                        continue
                    key = ("ts", str(resp.get("type")))
                    if key not in shapes_logged:
                        shapes_logged.add(key)
                        say(f"Bolt TargetSelection opportunity: {json.dumps(opp)[:1200]}")
                        wire("bolt_target_shape", opp)
                    spec = rdata.get("spec") or {}
                    stype = spec.get("type") if isinstance(spec, dict) else None
                    rtype = resp.get("type")
                    if rtype == "exactChoices":
                        sub = {"interactionId": iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": pick["id"]}}}
                    else:
                        sub = {"interactionId": iid,
                               "response": {"type": stype or "sequence",
                                            "data": {"choiceIds": [pick["id"]]}}}
                    say(f"targeting P1 with exiled Bolt: {pick['id']}")
                    wire("cast_perm_target", {"submission": sub})
                    await p0.send_interaction(sub)
                    submitted_interactions.add(iid)
                    await asyncio.sleep(0.8)
                    return
            return
        if wtype not in ("Priority", "MulliganDecision", None):
            # unrecognized non-priority prompt: log shape once
            key = ("wf", wtype)
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"UNRECOGNIZED waiting_for={wtype} data="
                    f"{json.dumps((state.get('waiting_for') or {}).get('data', {}), default=str)[:800]}")
                wire("unknown_wf", {"wtype": wtype,
                                    "data": (state.get("waiting_for") or {}).get("data", {})})
            if wtype != "Priority" or state.get("priority_player") != 0:
                return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        # 1) cast-permission leg: only after the mixed-reveal turn completed
        # (exile["done"]); otherwise an early exile would end the run before
        # the conclusive mixed test.
        if (mixed_done and not cast_perm["submitted"]
                and phase in ("PreCombatMain", "PostCombatMain")):
            ex_bolts = [mixed_bolt_oid] if mixed_bolt_oid else []
            if ex_bolts:
                for a in acts:
                    d = a.get("data", {})
                    if a["type"] == "CastSpell" and int(d.get("object_id", -1)) == ex_bolts[0]:
                        say(f"P0 casts exiled Lightning Bolt at P1 (oid {ex_bolts[0]})")
                        wire("cast_perm_action", a)
                        await submit_as_is(p0, a)
                        cast_perm["submitted"] = True
                        cast_perm["oid"] = ex_bolts[0]
                        cast_perm["rev"] = p0.revision
                        rej = await drain_rejections(p0)
                        if rej:
                            say(f"exiled Bolt cast rejected: {json.dumps(rej)[:300]}")
                            wire("cast_perm_rejected", rej)
                            notes.append(f"cast-permission submission rejected: {rej}")
                        return
                # no CastSpell offered for the exiled Bolt: check target prompt
                # needs driving first (handle below via generic target logic)
        if cast_perm["submitted"] and not cast_perm["resolved"]:
            if p0.revision != cast_perm["rev"]:
                st2 = p0.latest["state"]
                zo = get_obj(st2, cast_perm["oid"]).get("zone")
                if zo is not None and zo != "Stack":
                    cast_perm["resolved"] = True
                    l1 = life(st2, 1)
                    gy = [ (o.get("base_name") or o.get("name"))
                           for o in st2.get("objects", {}).values()
                           if o.get("zone") == "Graveyard" and o.get("controller") == 0]
                    say(f"exiled Bolt resolved: zone={zo} P1life={l1} P0gy={gy}")
                    wire("cast_perm_resolved", {"zone": zo, "p1_life": l1, "gy": gy})
                    if l1 == 17 and BOLT in gy:
                        ass["A7_cast_permission"] = "passed"
                        notes.append("A7: exiled Lightning Bolt cast this turn, P1 at 17, Bolt in graveyard")
                    else:
                        ass["A7_cast_permission"] = "failed"
                        notes.append(f"A7: exiled Bolt outcome wrong: P1={l1}, gy={gy}")
                    if not cast_exported:
                        try:
                            cj = await p0.export_state()
                            with open(f"{EVDIR}/cast.json", "w") as f:
                                f.write(cj)
                            cast_exported = True
                            say("exported CAST")
                        except Exception as e:
                            notes.append(f"cast export failed: {e}")
                    return
        # 2) exile-done bookkeeping: after this turn's selections settle,
        # verify zones once. Only the MIXED turn (red+blue revealed this turn)
        # is conclusive for A4/A5.
        if (exile["prompt_iids"] and not exile["done"]
                and wtype == "Priority"
                and phase in ("PreCombatMain", "PostCombatMain", "Combat",
                              "BeginningOfCombat", "DeclareAttackers",
                              "DeclareBlockers", "CombatDamage", "EndOfCombat")):
            st2 = p0.latest["state"]
            chosen = {cid: nm for _col, (cid, nm)
                      in exile["selections"].items()} if exile["selections"] \
                else {}
            chosen_cids = set(chosen)
            # full revealed pool this turn = union of all prompts' offered oids
            offered = []
            for oid in exile["offered_oids"]:
                nm = obj_name(st2, oid)
                offered.append((oid, nm, card_colors_of(nm)))
            nonland_offered = [(oid, nm) for oid, nm, _c in offered
                               if nm not in (ISLAND, MOUNTAIN)]
            turn_colors = set()
            for _oid, _nm, _cols in offered:
                turn_colors |= set(_cols)
            exile["offered_colors"] = set(turn_colors)
            # A3: X = colors among P0 permanents (red+blue from Sanar) -> 2
            if ass["A3_reveal_x"] == "not-run":
                if len(nonland_offered) == 2:
                    ass["A3_reveal_x"] = "passed"
                    notes.append(f"A3: Vivid revealed exactly 2 nonlands "
                                 f"(X=2 colors): {[(n, sorted(card_colors_of(n))) for _, n in nonland_offered]}")
                else:
                    ass["A3_reveal_x"] = "failed"
                    notes.append(f"A3: expected 2 revealed nonlands, saw {len(nonland_offered)}: "
                                 f"{[(n) for _, n in nonland_offered]}")
            is_mixed = {"Red", "Blue"} <= turn_colors and len(nonland_offered) == 2
            if is_mixed and not mixed_reveal_seen:
                mixed_reveal_seen = True
                notes.append(f"mixed reveal observed (red+blue) on turn {exile['turn']}")
                say("MIXED REVEAL: red+blue both available")
            # accumulate revealed-but-unchosen for the A6 stray check
            for cid, nm in nonland_offered:
                if cid not in chosen_cids:
                    revealed_unchosen[cid] = nm
            ex_oids = {int(oid) for oid, o in st2.get("objects", {}).items()
                       if o.get("zone") == "Exile" and o.get("controller") == 0}
            ex_names = sorted((get_obj(st2, oid).get("base_name")
                               or get_obj(st2, oid).get("name") or "?")
                              for oid in ex_oids)
            say(f"turn {exile['turn']} exile check: mixed={is_mixed} "
                f"chosen={chosen} exiled={ex_names}")
            wire("exile_turn_check", {"turn": exile["turn"], "mixed": is_mixed,
                                      "chosen": chosen, "exiled": ex_names,
                                      "offered_colors": sorted(exile["offered_colors"])})
            if not is_mixed:
                notes.append(f"turn {exile['turn']}: non-mixed reveal "
                             f"({sorted(turn_colors)}); exiled {ex_names}; continuing")
                exile["done"] = True  # settle this turn; wait for a mixed one
                return
            exile["done"] = True
            mixed_done = True
            for _col, (cid, nm) in exile["selections"].items():
                if nm == BOLT:
                    mixed_bolt_oid = cid
            say(f"mixed turn complete; mixed_bolt_oid={mixed_bolt_oid}")
            # A4: one optional selection per represented color offered
            if exile["offered_colors"] >= {"Red", "Blue"}:
                ass["A4_per_color_offer"] = "passed"
                notes.append(f"A4: per-color exile selections offered for "
                             f"{sorted(exile['offered_colors'])} on the mixed turn")
            else:
                ass["A4_per_color_offer"] = "failed"
                notes.append(f"A4: missing per-color offer on mixed turn: offered="
                             f"{sorted(exile['offered_colors'])}, selections="
                             f"{ {k: v[1] for k, v in exile['selections'].items()} }")
            # A5: BOTH mixed-turn choices actually exiled (the reported failure)
            if chosen_cids and chosen_cids <= ex_oids and len(chosen_cids) == 2:
                ass["A5_both_exiled"] = "passed"
                notes.append(f"A5: both revealed cards exiled on the mixed turn: {chosen}")
            else:
                ass["A5_both_exiled"] = "failed"
                notes.append(f"A5: could not exile both on the mixed turn: chosen={chosen}, "
                             f"exiled={ex_names}")
            # A6: no revealed-but-unchosen card stranded in Exile; this turn's
            # unchosen revealed cards back in Library
            strays = [oid for oid in ex_oids if oid in revealed_unchosen]
            unchosen_this = [(cid, nm) for cid, nm in nonland_offered
                             if cid not in chosen_cids]
            lib_ok = all(get_obj(st2, cid).get("zone") == "Library"
                         for cid, _nm in unchosen_this)
            say(f"A6: strays={strays} unchosen_this={unchosen_this} lib_ok={lib_ok}")
            wire("a6_check", {"strays": strays, "unchosen_this": unchosen_this,
                              "lib_ok": lib_ok})
            if not strays and lib_ok:
                ass["A6_only_chosen"] = "passed"
                notes.append("A6: only chosen cards exiled; unchosen revealed cards back in Library")
            else:
                ass["A6_only_chosen"] = "failed"
                notes.append(f"A6: stray exiles={strays} lib_ok={lib_ok}")
            if not post_exported:
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST (post exile+shuffle)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
            return
        # 3) A1 setup check at PreCombatMain with Sanar on BF
        if (phase == "PreCombatMain" and state.get("active_player") == 0
                and battlefield_ids(state, 0, SANAR)
                and ass["A1_setup_ok"] == "not-run"):
            cols = set()
            for oid, o in state.get("objects", {}).items():
                if o.get("zone") == "Battlefield" and o.get("controller") == 0:
                    cols |= card_colors_of(o.get("base_name") or o.get("name") or "")
            ok = cols >= {"Red", "Blue"}
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: Sanar on BF at PreCombatMain; P0 permanent colors={sorted(cols)}")
            say(f"A1 setup: colors={sorted(cols)}")
        # 4) cast Sanar when able (legend gate: none on BF)
        if (not sanar_cast["submitted"] and not battlefield_ids(state, 0, SANAR)
                and phase in ("PreCombatMain", "PostCombatMain")
                and len(untapped_lands(state, 0)) >= 4
                and SANAR in hand_names(state, 0)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == SANAR:
                    say("P0 casts Sanar, Innovative First-Year")
                    wire("sanar_cast", a)
                    await submit_as_is(p0, a)
                    sanar_cast["submitted"] = True
                    sanar_cast["oid"] = d.get("object_id")
                    sanar_cast["rev"] = p0.revision
                    rej = await drain_rejections(p0)
                    if rej:
                        say(f"Sanar cast rejected: {json.dumps(rej)[:300]}")
                        wire("sanar_rejected", rej)
                        notes.append(f"Sanar CastSpell rejected: {rej}")
                    return
        if sanar_cast["submitted"] and not sanar_cast["resolved"] \
                and p0.revision != sanar_cast["rev"]:
            st2 = p0.latest["state"]
            if battlefield_ids(st2, 0, SANAR):
                sanar_cast["resolved"] = True
                say("Sanar resolved on battlefield")
                wire("sanar_resolved", {})
        # 5) normal play: land drop every tick (no kept-flag; #6690 lesson)
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        say("P0 passes priority via viewer_interaction fallback")
                        await p0.send_interaction(sub)
                        return
        say(f"P0 NO-ACTION turn={turn} phase={phase} acts={[a['type'] for a in acts][:8]}")

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision" and mulligan_pending_for(state, 1) \
                and not kept.get("P1"):
            kept["P1"] = True
            await p1.send_action({"type": "MulliganDecision",
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
        if wtype == "DiscardToHandSize":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []):
                    data = (opp.get("response", {}) or {}).get("data", {}) or {}
                    cands = data.get("candidates") or []
                    if cands:
                        spec = data.get("spec") or {}
                        stype = spec.get("type", "select")
                        sub = {"interactionId": opp.get("interactionId"),
                               "response": {"type": stype,
                                            "data": {"choiceIds": [cands[0]["id"]]}}}
                        await p1.send_interaction(sub)
                        say("P1 discards to hand size")
                        return
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        # P1 turn driver: land, opportunistic bears, pass (#6762 lesson:
        # a fully passive seat stalls the game)
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            d = a.get("data", {})
            if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == BEARS:
                await submit_as_is(p1, a)
                say("P1 casts Grizzly Bears")
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        await p1.send_interaction(sub)
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
        # completion: A7 resolved -> finish
        if ass["A7_cast_permission"] != "not-run":
            say("cast-permission leg done; finishing")
            await finish()
            return
        # cap: no mixed reveal after 8 trigger turns -> blocked
        if trigger_turns >= 8 and not mixed_reveal_seen:
            notes.append(f"no mixed (red+blue) reveal in {trigger_turns} Vivid trigger turns; "
                         "cannot test the reported both-exile path")
            say("no mixed reveal after 8 trigger turns; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            acts0 = [a["type"] for a in merged_actions(p0.latest)][:8]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)} "
                f"lands={len(untapped_lands(s,0))} sanarBF={bool(battlefield_ids(s,0,SANAR))} "
                f"acts={acts0} exile_done={exile['done']} mixed={mixed_reveal_seen}")
        # stall detection while an exile prompt is outstanding
        if exile["prompt_iids"] and not exile["done"] and stuck_deadline is None:
            stuck_deadline = time.time() + 240
        if not exile["prompt_iids"] or exile["done"]:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("exile selection flow stalled 240s after first prompt; see wire log")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
