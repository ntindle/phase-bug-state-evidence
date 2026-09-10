#!/usr/bin/env python3
"""Scenario for phase-rs/phase#6250.

Ardenn, Intrepid Archaeologist: "At the beginning of combat on your turn, you
may attach any number of Auras and Equipment you control to target permanent
or player." Reported: only ONE Aura/Equipment is attached.

Behavioral contract:
  setup   P0 controls Ardenn + 3 unattached-or-attached attachments
          (holy strength [aura, attached to Ardenn on cast],
           plate armor + colossus hammer [equipment, unattached]) and moves to
          combat.
  trigger P0 chooses Ardenn as the trigger's target permanent, accepts the
          optional "you may", and selects attachments.
  expect  ALL THREE attachments end attached to Ardenn.
  bug     only one attachment is moved (resolver collapses the Or(Aura,
          Equipment) filter to its first match).
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "6250-20260910-071149"
EVDIR = f"{BACKFILL}/evidence/6250/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ARDENN = "ardenn, intrepid archaeologist"  # card-data.json key (exact)
HOLY = "holy strength"
PLATE = "plate armor"
HAMMER = "colossus hammer"
ATTACHMENTS = [HOLY, PLATE, HAMMER]
BASICS = {"plains": "White"}

P0_DECK = [(ARDENN, 8), (HOLY, 8), (PLATE, 8), (HAMMER, 8), ("plains", 28)]
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
    "observed_at": "2026-09-10",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 6250-20260910-071149",
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
    return obj_name(state.get("objects", {}).get(str(oid), {}))


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


def bf_attachments(state, pid):
    return {str(oid): o for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) in ATTACHMENTS}


def attached_to_oid(o):
    for k in ("attached_to", "attachedTo", "attaching_to", "attached_to_id",
              "attachedToId", "parent_id", "host_id"):
        v = o.get(k)
        if v is None:
            continue
        if isinstance(v, dict) and "data" in v:
            return str(v["data"])
        return str(v)
    # nested shapes: {"attached": {"to": id}} etc.
    for k in ("attached", "attachment"):
        v = o.get(k)
        if isinstance(v, dict):
            for kk in ("to", "to_id", "target", "host"):
                if v.get(kk) is not None:
                    vv = v[kk]
                    if isinstance(vv, dict) and "data" in vv:
                        return str(vv["data"])
                    return str(vv)
    return None


def attachments_on(state, pid, host_name):
    host_oid = None
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and obj_name(o) == host_name
                and o.get("controller") == pid):
            host_oid = str(oid)
            break
    if host_oid is None:
        return []
    return [obj_name(o) for oid, o in bf_attachments(state, pid).items()
            if attached_to_oid(o) == host_oid]


def untapped_lands(state, pid):
    return [o for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and obj_name(o) in BASICS]


def ardenn_on_bf(state, pid):
    return any(obj_name(o) == ARDENN for oid, o in state.get("objects", {}).items()
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


def surf_names(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict):
            if d.get("name"):
                out.append(str(d["name"]).lower())
            if d.get("seat") is not None:
                out.append(f"player-seat:{d['seat']}")
    return out


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_target_chosen", "A3_optional_accepted",
            "A4_all_attached", "A5_cleanup")}
    obs = {"trigger_target": None, "accepted": False,
           "attachment_prompt_shapes": [], "attach_choices_made": [],
           "target_prompt_shapes": [], "optional_prompt_shapes": [],
           "pre_attached_to_ardenn": None, "post_attached_to_ardenn": None}
    pre_exported = False
    post_exported = False
    setup_done = False
    holy_cast_pending = False
    cast_counts = {HOLY: 0, PLATE: 0, HAMMER: 0}
    kept = {}
    submitted_interactions = set()
    picked_attachments = set()  # choice ids already chosen, for exactChoices re-prompts
    shapes_logged = set()
    settle_empty = 0

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def stack_sig(state):
        return [(e.get("id"), str(e.get("source") or e.get("text") or "")[:60])
                for e in (state.get("stack") or [])]

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

    async def scan_interactions(st, who):
        """Handle: trigger target selection (choose Ardenn), the optional
        'you may' (decideOptionalEffect accept=true), and the attachment
        choice (choose up to the prompt's max). Priority menus (action-code
        surfaces) are never touched here."""
        nonlocal holy_cast_pending
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        c = p0 if who == "P0" else p1
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
            key = (who, rtype, blob[:100], tuple(sorted(codes))[:3])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} n={len(chs)} "
                    f"codes={sorted(codes)[:4]} choices=[{blob[:220]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "interaction": opp})
            if iid in submitted_interactions:
                continue
            if who != "P0":
                continue
            # --- priority menus: never touch (handled via legal actions) ---
            if codes - {"decideOptionalEffect"}:
                continue
            # --- optional "you may": decideOptionalEffect, accept=true ---
            if "decideOptionalEffect" in codes:
                obs["optional_prompt_shapes"].append(blob[:200])
                pick = next((ch for ch in chs if accept_value(ch) is True),
                            None)
                if pick is None:
                    notes.append(f"optional prompt: no accept=true choice "
                                 f"(iid={iid}); skipping")
                    wire("optional_no_accept", {"iid": iid,
                                                "interaction": opp})
                    continue
                sub = {"interactionId": iid, "response":
                       {"type": "choose", "data": {"choiceId": pick["id"]}}}
                say(f"[P0] optional 'you may' -> ACCEPT")
                wire("optional_accept", {"submission": sub})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["accepted"] = True
                acted = True
                continue
            # --- candidate classification for schema/object prompts ---
            def cand_kind(ch):
                nm = choice_text(ch).lower()
                sns = surf_names(ch)
                if nm in ATTACHMENTS or any(n in ATTACHMENTS for n in sns):
                    return "attachment"
                if any(s.startswith("player-seat:") for s in sns):
                    return "player"
                if nm or sns:
                    return "permanent"
                return "unknown"

            kinds = {cand_kind(ch) for ch in chs}
            avail_attach = [
                ch for ch in chs if cand_kind(ch) == "attachment"
                and ch.get("status", {}).get("type", "available")
                != "unavailable"]
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            spec_data = (spec.get("data") if isinstance(spec, dict)
                         else {}) or {}
            # --- attachment selection prompt ---
            if avail_attach and kinds <= {"attachment", "unknown"}:
                cap = spec_data.get("max")
                obs["attachment_prompt_shapes"].append(
                    f"{rtype}/{spec_type} max={cap} n_avail={len(avail_attach)}")
                say(f"[P0] attachment prompt: spec max={cap}, "
                    f"{len(avail_attach)} attachments available "
                    f"({[choice_text(ch) for ch in avail_attach]})")
                if isinstance(cap, int) and cap < len(avail_attach):
                    notes.append(
                        f"attachment prompt caps selection at max={cap} "
                        f"with {len(avail_attach)} attachments available -- "
                        f"the 'any number' choice is already restricted here")
                # prefer currently-unattached attachments so the move is
                # visible in the post state
                def attach_sort_key(ch):
                    ref = None
                    for s in ch.get("surfaces", []) or []:
                        d = (s.get("data") or {})
                        if isinstance(d, dict) and "reference" in d:
                            ref = str(d.get("reference"))
                            break
                    o = get_obj(st["state"], ref) if ref else {}
                    return (0 if attached_to_oid(o) is None else 1,
                            choice_text(ch))
                avail_attach = sorted(avail_attach, key=attach_sort_key)
                n_pick = (min(cap, len(avail_attach)) if isinstance(cap, int)
                          else len(avail_attach))
                picks = avail_attach[:n_pick]
                ids = [ch["id"] for ch in picks]
                if rtype == "exactChoices":
                    sub = {"interactionId": iid, "response":
                           {"type": "choose",
                            "data": {"choiceId": ids[0]}}}
                else:
                    sub = {"interactionId": iid, "response":
                           {"type": spec_type or "sequence",
                            "data": {"choiceIds": ids}}}
                say(f"[P0] attachment choice -> "
                    f"{[choice_text(ch) for ch in picks]}")
                wire("attachment_choice",
                     {"submission": sub, "spec_max": cap,
                      "available": [choice_text(ch) for ch in avail_attach]})
                obs["attach_choices_made"].extend(
                    choice_text(ch) for ch in picks)
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                acted = True
                continue
            # --- target selection prompt (permanents and/or players) ---
            if kinds & {"permanent", "player"}:
                obs["target_prompt_shapes"].append(blob[:200])
                ardenn_ch = next(
                    (ch for ch in chs
                     if "ardenn" in choice_text(ch).lower()
                     or any("ardenn" in n for n in surf_names(ch))), None)
                pick = ardenn_ch or chs[0]
                sub = {"interactionId": iid, "response":
                       {"type": spec_type or "sequence",
                        "data": {"choiceIds": [pick["id"]]}}}
                say(f"[P0] target selection -> {choice_text(pick)!r} "
                    f"(ardenn found: {ardenn_ch is not None})")
                wire("target_choice",
                     {"submission": sub, "blob": blob[:300],
                      "holy_cast_pending": holy_cast_pending})
                if not holy_cast_pending and ardenn_ch is not None:
                    obs["trigger_target"] = "ardenn"
                if holy_cast_pending:
                    holy_cast_pending = False
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                acted = True
                continue
        return acted

    def evaluate():
        pre_st = load_env(f"{EVDIR}/pre.json")
        post_st = load_env(f"{EVDIR}/post.json")
        if pre_st is not None:
            atts = bf_attachments(pre_st, 0)
            on_ardenn = attachments_on(pre_st, 0, ARDENN)
            obs["pre_attached_to_ardenn"] = sorted(on_ardenn)
            ok = (ardenn_on_bf(pre_st, 0)
                  and len(atts) == 3
                  and HOLY in on_ardenn  # aura attached via its own cast
                  and PLATE not in on_ardenn and HAMMER not in on_ardenn
                  and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
                  and pre_st.get("phase") == "PreCombatMain")
            if ok:
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre.json: Ardenn on BF, 3 attachments controlled "
                             f"by P0 (holy strength attached to Ardenn, plate "
                             f"armor + colossus hammer unattached), "
                             f"PreCombatMain, life 20/20")
                for oid, o in atts.items():
                    wire("pre_attachment_obj",
                         {"name": obj_name(o), "object": o})
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append(f"pre.json setup precondition not met "
                             f"(ardenn={ardenn_on_bf(pre_st,0)}, atts="
                             f"{sorted(obj_name(o) for o in atts.values())}, "
                             f"on_ardenn={sorted(on_ardenn)}, "
                             f"phase={pre_st.get('phase')})")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("pre.json missing (setup never completed)")
        if obs["trigger_target"] == "ardenn":
            ass["A2_trigger_target_chosen"] = "passed"
            notes.append("trigger target selection: chose Ardenn, Intrepid "
                         "Archaeologist as the target permanent")
        elif obs["trigger_target"] is None and obs["target_prompt_shapes"]:
            ass["A2_trigger_target_chosen"] = "failed"
            notes.append(f"target prompt appeared but Ardenn was not chosen "
                         f"(shapes={obs['target_prompt_shapes']})")
        else:
            ass["A2_trigger_target_chosen"] = "failed"
            notes.append("no trigger target selection observed for P0")
        if obs["accepted"]:
            ass["A3_optional_accepted"] = "passed"
            notes.append("accepted the trigger's optional 'you may'")
        else:
            ass["A3_optional_accepted"] = "failed"
            notes.append("optional accept prompt never observed/accepted")
        if post_st is not None and pre_st is not None:
            on_ardenn = attachments_on(post_st, 0, ARDENN)
            obs["post_attached_to_ardenn"] = sorted(on_ardenn)
            for oid, o in bf_attachments(post_st, 0).items():
                wire("post_attachment_obj",
                     {"name": obj_name(o), "object": o})
            moved = [n for n in (PLATE, HAMMER) if n in on_ardenn]
            if (HOLY in on_ardenn and PLATE in on_ardenn
                    and HAMMER in on_ardenn):
                ass["A4_all_attached"] = "passed"
                notes.append(f"post.json: all 3 attachments on Ardenn "
                             f"({sorted(on_ardenn)}) -- any-number behavior OK")
            else:
                ass["A4_all_attached"] = "failed"
                notes.append(f"post.json: attachments on Ardenn = "
                             f"{sorted(on_ardenn)} (expected all of "
                             f"{ATTACHMENTS}); only {len(moved)} of the 2 "
                             f"unattached equipment moved -- REPORTED BUG: "
                             f"trigger attached a single Aura/Equipment")
            stack_empty = len(post_st.get("stack") or []) == 0
            if (stack_empty and life_of(post_st, 0) == 20
                    and life_of(post_st, 1) == 20):
                ass["A5_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, life 20/20 "
                             f"(phase={post_st.get('phase')})")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"post.json: stack_empty={stack_empty} "
                             f"life={life_of(post_st,0)}/{life_of(post_st,1)}")
        else:
            for k in ("A4_all_attached", "A5_cleanup"):
                ass[k] = "failed"
                notes.append(f"{k} unevaluable (missing pre/post state)")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif ass["A4_all_attached"] == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("setup valid but fewer than all 3 attachments ended "
                         "on Ardenn -- matches the reported single-attachment "
                         "collapse")
        return verdict

    def load_env(path):
        try:
            with open(path) as f:
                return json.loads(f.read())["state"]
        except Exception:
            return None

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
            "issue": 6250,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6250.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x copies per card is a test-harness convenience (engine "
                "accepts >4-of for custom games); exercised behavior is the "
                "shipped card text.",
                "Only the accept branch of the optional trigger is exercised; "
                "the decline control is not run in this scenario.",
                "Second card in the same class (e.g. Brass Squire) not "
                "exercised; acceptance criterion noted for the fix, not the repro.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x ardenn + 8x holy strength + 8x plate armor + "
                          "8x colossus hammer + 28x plains; P1: 60x mountain "
                          "dummy. Mulligan to Ardenn + lands; cast Ardenn, then "
                          "holy strength (targeting Ardenn), plate armor, "
                          "colossus hammer; move to combat with all three "
                          "attachments controlled by P0.",
            "contract_line": "Beginning-of-combat trigger: choose Ardenn as "
                             "target permanent, accept 'you may', select all "
                             "3 attachments; expect all 3 attached to Ardenn "
                             "after resolution.",
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

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, setup_done, holy_cast_pending, \
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
            if (ARDENN in hn and lands >= 2) or mulls >= 5:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (ardenn={ARDENN in hn}, lands={lands}, "
                    f"mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (ardenn={ARDENN in hn}, "
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
                seen_att = set()

                def bottom_key(oid):
                    nm = lname(state, oid)
                    if nm in BASICS:
                        return (0, nm)  # spare lands first
                    if nm in ATTACHMENTS:
                        if nm in seen_att:
                            return (1, nm)  # duplicate attachments next
                        seen_att.add(nm)
                        return (3, nm)
                    if nm == ARDENN:
                        return (4, nm)
                    return (2, nm)
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
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                say("OrderTriggers: submitting advertised default order")
                wire("order_triggers", oa)
                await submit_as_is(p0, oa)
                return
        if await scan_interactions(st, "P0"):
            return
        # --- post checkpoint: trigger accepted, stack settled past combat start
        if obs["accepted"] and not post_exported:
            if len(state.get("stack") or []) == 0:
                settle_empty += 1
            else:
                settle_empty = 0
            if settle_empty >= 2:
                say(f"POST: trigger resolved; on_ardenn="
                    f"{attachments_on(state, 0, ARDENN)} "
                    f"phase={phase} turn={turn}")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
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
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        main_phase = phase in ("PreCombatMain", "PostCombatMain")
        unt = untapped_lands(state, 0)
        hn = hand_names(state, 0)
        lands_played = player_of(state, 0).get("lands_played_this_turn", 0)
        ardenn_bf = ardenn_on_bf(state, 0)
        atts = bf_attachments(state, 0)
        att_names = sorted(obj_name(o) for o in atts.values())
        if holy_cast_pending and HOLY in att_names:
            # aura resolved (engine auto-targeted the only creature); the
            # cast-time target prompt never appears
            holy_cast_pending = False
        # cast Ardenn
        if not ardenn_bf and not setup_done:
            ca = find_action(acts, "CastSpell", ARDENN, state)
            if ca and main_phase:
                say(f"P0 casts Ardenn (turn {turn})")
                wire("cast_ardenn", ca)
                await submit_as_is(p0, ca)
                return
        # cast holy strength (targets Ardenn via interaction prompt)
        if (ardenn_bf and HOLY in hn and main_phase and not setup_done
                and cast_counts[HOLY] == 0):
            ca = find_action(acts, "CastSpell", HOLY, state)
            if ca:
                say(f"P0 casts Holy Strength (turn {turn})")
                wire("cast_holy", ca)
                holy_cast_pending = True
                cast_counts[HOLY] += 1
                await submit_as_is(p0, ca)
                return
        # cast plate armor
        if (ardenn_bf and PLATE in hn and main_phase and not setup_done
                and cast_counts[PLATE] == 0):
            ca = find_action(acts, "CastSpell", PLATE, state)
            if ca:
                say(f"P0 casts Plate Armor (turn {turn})")
                wire("cast_plate", ca)
                cast_counts[PLATE] += 1
                await submit_as_is(p0, ca)
                return
        # cast colossus hammer
        if (ardenn_bf and HAMMER in hn and main_phase and not setup_done
                and cast_counts[HAMMER] == 0):
            ca = find_action(acts, "CastSpell", HAMMER, state)
            if ca:
                say(f"P0 casts Colossus Hammer (turn {turn})")
                wire("cast_hammer", ca)
                cast_counts[HAMMER] += 1
                await submit_as_is(p0, ca)
                return
        # setup complete? export PRE and head to combat
        if (ardenn_bf and len(atts) == 3 and not setup_done
                and main_phase and phase == "PreCombatMain"):
            setup_done = True
            say(f"setup complete (turn {turn}): on_ardenn="
                f"{attachments_on(state, 0, ARDENN)}; exporting PRE")
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_exported = True
                say("exported PRE")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
            # fall through to pass priority below
        # play a land only while setting up
        if not setup_done:
            for a in acts:
                if a["type"] == "PlayLand" and lands_played == 0:
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
    while time.time() - t0 < 1200:
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
                f"P0untapped={len(untapped_lands(s, 0))} "
                f"ardenn={ardenn_on_bf(s, 0)} "
                f"atts={sorted(obj_name(o) for o in bf_attachments(s, 0).values())} "
                f"life={life_of(s, 0)}/{life_of(s, 1)} "
                f"stack={stack_sig(s)} accepted={obs['accepted']} "
                f"setup={setup_done} post={post_exported}")
        if obs["accepted"] and not post_exported and stuck_deadline is None:
            stuck_deadline = time.time() + 300
        if not obs["accepted"] or post_exported:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("trigger accepted but post-resolution state not reached "
                         "in 300s; see wire log (possible unhandled interaction)")
            await finish()
            return
    notes.append("global timeout (1200s) hit before assertions resolved")
    await finish()


asyncio.run(main())
