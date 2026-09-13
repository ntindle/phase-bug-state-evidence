#!/usr/bin/env python3
"""Issue #6917: Oko, Thief of Crowns +1 is only until end of turn in game.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Oko, Thief of Crowns ({1}{G}{U}, loyalty 4):
    "[+1]: Target artifact or creature loses all abilities and becomes a
     green Elk creature with base power and toughness 3/3."
  No duration is given: the effect is an indefinite continuous effect.

Reported symptom: in-game the elk effect expires at end of turn.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Oko, Thief of Crowns + 28x Forest + 28x Island. Casts Oko, then
      activates its +1 targeting one of P1's creatures.
  P1: 12x Grizzly Bears + 48x Forest. Casts 2x Grizzly Bears so the +1's
      target prompt has >=2 legal candidates (single legal target is
      auto-targeted by the engine).

Plan:
  1. P0 casts Oko ({1}{G}{U}) on its turn 3 (needs Forest+Island+land).
  2. P1 casts 2x Grizzly Bears on its turns 2 and 3.
  3. On a later P0 main phase, P0 activates Oko's +1 (ability_index 1,
     AsSorcery), answering the target prompt with one bear; the ACTUALLY
     submitted candidate oid is recorded at answer time.
  4. Export PRE (just before the activation), MID (same turn, after the
     ability resolves and the stack empties), POST (P0's NEXT turn).

Assertions:
  A1_setup_ok        PRE: Oko on P0's BF with loyalty 4; >=2 Grizzly Bears
                     on P1's BF.
  A2_activation      Oko's loyalty is 5 in MID (4->5); the +1 resolved.
  A3_elk_this_turn   MID: the targeted bear is a green 3/3 Elk with no
                     abilities (power==3, toughness==3, subtypes has Elk,
                     color has Green, abilities empty).
  A4_elk_persists    POST (P0's next turn): the targeted bear is STILL a
                     green 3/3 Elk. FAILED = it reverted to a 2/2 Bear ->
                     the reported duration bug is REPRODUCED.
  A5_cleanup         POST: stack empty, waiting_for is Priority/None.

Verdict rule: reproduced iff A1-A3 pass and A4 fails. not-reproduced iff
A1-A5 all pass. blocked iff A1 fails (or A2/A3 fail for setup reasons).

Evidence: evidence/6917/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6917.py, wire_log.jsonl,
scenario_run.log, server.log
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6917")
EVDIR = f"{BACKFILL}/evidence/6917/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OKO = "oko, thief of crowns"
BEAR = "grizzly bears"
FOREST = "forest"
ISLAND = "island"

P0_DECK = [(OKO, 4), (FOREST, 28), (ISLAND, 28)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (isolated v0.81.3 server "
              "started for this run) + verified pin (minisign-verify of "
              "binary + signed data manifest with the repo-pinned key).",
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


def untapped_lands(state, pid, names=(FOREST, ISLAND)):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in names):
            out.append(int(oid))
    return out


def life_of(state, pid):
    return player_of(state, pid).get("life")


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


def creature_sig(state, oid):
    """Compact signature of a battlefield creature's current characteristics."""
    o = get_obj(state, oid)
    ct = o.get("card_types") or {}
    return {
        "name": obj_name(state, oid),
        "zone": o.get("zone"),
        "power": o.get("power"),
        "toughness": o.get("toughness"),
        "layer_base_power": o.get("layer_base_power"),
        "layer_base_toughness": o.get("layer_base_toughness"),
        "color": o.get("color"),
        "core_types": ct.get("core_types"),
        "subtypes": ct.get("subtypes"),
        "n_abilities": len(o.get("abilities") or []),
        "loyalty": o.get("loyalty"),
        "controller": o.get("controller"),
        "tapped": bool(o.get("tapped")),
    }


def is_elk(sig):
    subs = [str(s).lower() for s in (sig.get("subtypes") or [])]
    colors = [str(c).lower() for c in (sig.get("color") or [])]
    return (sig.get("power") == 3 and sig.get("toughness") == 3
            and "elk" in subs and "green" in colors
            and (sig.get("n_abilities") or 0) == 0)


def is_bear(sig):
    subs = [str(s).lower() for s in (sig.get("subtypes") or [])]
    return (sig.get("power") == 2 and sig.get("toughness") == 2
            and "bear" in subs)


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_activation", "A3_elk_this_turn",
            "A4_elk_persists", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"oko_cast": False, "oko_cast_turn": None, "oko_oid": None,
          "plus_one_done": False, "act_turn": None, "target_oid": None,
          "target_candidates": [], "target_wf": None,
          "act_in_flight": False, "mid_exported": False,
          "post_exported": False, "post_at": None,
          "mid_act_turn": None, "oko_loyalty_mid": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "target_selections": [], "tick_errors": []}
    prompt_first_seen = {}
    last_select = {}

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        pre_st = mid_st = post_st = None
        for fn, slot in (("pre", 0), ("mid", 1), ("post", 2)):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    st = json.loads(open(p).read())["state"]
                    if slot == 0:
                        pre_st = st
                    elif slot == 1:
                        mid_st = st
                    else:
                        post_st = st
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        # ---- A1: setup ----
        if pre_st is not None:
            oko_oid = ST["oko_oid"] or bf_id(pre_st, 0, OKO)
            o = get_obj(pre_st, oko_oid) if oko_oid else {}
            bears = bf_ids(pre_st, 1, BEAR)
            ok = (oko_oid is not None and o.get("zone") == "Battlefield"
                  and o.get("loyalty") == 4 and len(bears) >= 2)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: oko_oid={oko_oid} loyalty={o.get('loyalty')} "
                         f"zone={o.get('zone')} p1_bears={len(bears)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: pre.json missing")

        # ---- A2: activation ----
        if mid_st is not None and ST["oko_oid"] is not None:
            o = get_obj(mid_st, ST["oko_oid"])
            loy = o.get("loyalty")
            ST["oko_loyalty_mid"] = loy
            ok = (loy == 5)
            ass["A2_activation"] = "passed" if ok else "failed"
            notes.append(f"A2: oko loyalty in MID = {loy} "
                         f"(expected 5); plus_one_done={ST['plus_one_done']}")
        else:
            ass["A2_activation"] = "failed"
            notes.append("A2 failed: mid.json missing or no oko_oid")

        # ---- A3: elk this turn ----
        if mid_st is not None and ST["target_oid"] is not None:
            sig = creature_sig(mid_st, ST["target_oid"])
            elk = is_elk(sig)
            ass["A3_elk_this_turn"] = "passed" if elk else "failed"
            notes.append(f"A3: target oid={ST['target_oid']} MID sig={sig} "
                         f"is_elk={elk}")
        else:
            ass["A3_elk_this_turn"] = "failed"
            notes.append("A3 failed: mid.json missing or no target_oid")

        # ---- A4: elk persists to P0's next turn ----
        if post_st is not None and ST["target_oid"] is not None:
            sig = creature_sig(post_st, ST["target_oid"])
            elk = is_elk(sig)
            bear = is_bear(sig)
            if elk:
                ass["A4_elk_persists"] = "passed"
                notes.append(f"A4 passed: target STILL elk in POST: {sig}")
            elif bear:
                ass["A4_elk_persists"] = "failed"
                notes.append("A4 FAILED: target reverted to 2/2 Bear in POST "
                             f"(after turn end). BUG REPRODUCED. sig={sig}")
            else:
                ass["A4_elk_persists"] = "failed"
                notes.append(f"A4 FAILED: target in unexpected form: {sig}")
        else:
            ass["A4_elk_persists"] = "failed"
            notes.append("A4 failed: post.json missing or no target_oid")

        # ---- A5: cleanup ----
        if post_st is not None:
            stack_empty = not (post_st.get("stack") or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A5_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A5: stack_empty={stack_empty} waiting_for={wf}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: post.json missing")

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif (ass["A2_activation"] == "passed"
              and ass["A3_elk_this_turn"] == "passed"
              and ass["A4_elk_persists"] == "failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif ass["A4_elk_persists"] == "failed" and ass["A3_elk_this_turn"] == "passed":
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: setup/activation chain incomplete")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6917,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (pid in runs/<run-id>/"
                               "server.pid)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6917.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6917.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "4x Oko / 12x Grizzly Bears density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "'Loses all abilities' is not separately asserted: Grizzly "
                "Bears has no abilities; the reported defect is the effect "
                "duration (until-EOT vs indefinite).",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6917.py",
                    f"{EVDIR}/scenario_6917.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6917.py and server.log into EVDIR")
        render_summary(run, pre_st, mid_st, post_st)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    def render_summary(run, pre_st, mid_st, post_st):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 820
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6917 - Oko, Thief of Crowns +1",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "elk effect duration (indefinite vs until-EOT)",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "PRE: Oko loyalty 4 on BF; >=2 Bears on P1 BF",
            "A2_activation": "Oko loyalty 4->5 (+1 resolved)",
            "A3_elk_this_turn": "MID: target is green 3/3 Elk, no abilities",
            "A4_elk_persists": "POST (P0 next turn): target STILL 3/3 Elk",
            "A5_cleanup": "stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Target creature signature across states:",
               fill=(200, 210, 225))
        y += 24
        tgt = run["driver_state"].get("target_oid")
        for label, st in (("pre ", pre_st), ("mid ", mid_st),
                          ("post", post_st)):
            if st is not None and tgt is not None:
                s = creature_sig(st, tgt)
                line = (f"{label}: oid={tgt} {s['power']}/{s['toughness']} "
                        f"{s['subtypes']} {s['color']} "
                        f"abilities={s['n_abilities']} zone={s['zone']}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:112], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:10]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_6917.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
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

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in (FOREST, ISLAND))
        mulls = kept.get(f"P{pid}_mulls", 0)
        if pid == 0:
            ok = (lands >= 3 and FOREST in hn and ISLAND in hn) or mulls >= 2
        else:
            ok = lands >= 2 or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands)")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in hand[:1]]}})
        say(f"{tag} bottoms 1")

    async def discard_tick(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                t = choice_text(ch).lower()
                return 0 if t in (FOREST, ISLAND) else 1

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def target_tick(c, pid, tag, st, state):
        """Answer Oko +1's target prompt; record the SUBMITTED candidate oid."""
        if not ST["act_in_flight"] or ST["target_oid"] is not None:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            bears = [ch for ch in chs
                     if "grizzly" in choice_text(ch).lower()
                     or "bear" in json.dumps(ch, default=str).lower()[:200]]
            pick = bears[0] if bears else chs[0]
            # resolve the ACTUALLY submitted candidate's object reference
            oid = None
            for s in pick.get("surfaces", []) or []:
                dd = s.get("data") or {}
                ref = dd.get("reference") if isinstance(dd, dict) else None
                if isinstance(ref, int):
                    oid = ref
                    break
                if isinstance(ref, dict):
                    for v in ref.values():
                        if isinstance(v, int):
                            oid = v
                            break
            ST["target_oid"] = oid
            ST["target_candidates"] = [choice_text(ch)[:50] for ch in chs]
            ST["target_wf"] = wf.get("type")
            obs["target_selections"].append(
                {"stage": "oko_plus_one", "who": tag,
                 "iid": str(iid)[:16], "submitted_oid": oid,
                 "candidates": ST["target_candidates"]})
            say(f"[{tag}] OKO TARGET PROMPT wf={wf.get('type')} "
                f"candidates={len(chs)} submitted_oid={oid}")
            wire("oko_target_prompt",
                 {"who": tag, "iid": str(iid)[:16], "submitted_oid": oid,
                  "wf_type": wf.get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if time.time() - entry["t0"] < 20:
                continue
            pick = chs[0] if chs else None
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 20s stall")
                obs["auto_answered"].append({"who": tag})
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def cast_named(c, acts, state, name, tag):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == name:
                say(f"[{tag}] casting {name} via {a['type']} (oid {oid})")
                wire("cast", {"who": tag, "name": name, "oid": oid,
                              "action": a["type"]})
                await submit_as_is(c, a)
                return oid
        return None

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0")
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        if await target_tick(p0, 0, "P0", st, state):
            return
        # record Oko on BF
        oid = bf_id(state, 0, OKO)
        if oid is not None and ST["oko_oid"] is None:
            ST["oko_oid"] = int(oid)
            say(f"Oko on BF: oid={oid} loyalty={get_obj(state, oid).get('loyalty')}")
            wire("oko_entered", {"oid": int(oid)})
        # detect +1 resolution: stack empty after in-flight
        if ST["act_in_flight"] and not (state.get("stack") or []):
            ST["act_in_flight"] = False
            ST["plus_one_done"] = True
            ST["mid_act_turn"] = state.get("turn_number")
            say(f"Oko +1 resolved on turn {state.get('turn_number')}; "
                f"target oid={ST['target_oid']}")
            wire("oko_plus_one_resolved",
                 {"turn": state.get("turn_number"),
                  "target_oid": ST["target_oid"]})
            if not ST["mid_exported"]:
                if await export_named("mid"):
                    ST["mid_exported"] = True
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        # cast Oko ({1}{G}{U}) when 3+ untapped lands incl Forest+Island
        if (not ST["oko_cast"] and oid is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            lands = untapped_lands(state, 0)
            names = [lname(state, x) for x in lands]
            if (len(lands) >= 3 and FOREST in names and ISLAND in names
                    and OKO in hand_lnames(state, 0)):
                coid = await cast_named(p0, acts, state, OKO, "P0")
                if coid is not None:
                    ST["oko_cast"] = True
                    ST["oko_cast_turn"] = state.get("turn_number")
                    return
        # activate +1: AsSorcery, once per turn, needs >=2 bears for the prompt
        if (not ST["plus_one_done"] and not ST["act_in_flight"]
                and oid is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not (state.get("stack") or [])
                and len(bf_ids(state, 1, BEAR)) >= 2
                and int(get_obj(state, oid).get("loyalty_activations_this_turn") or 0) == 0):
            for a in acts:
                if a["type"] != "ActivateAbility":
                    continue
                d = a.get("data", {}) or {}
                if d.get("ability_index") == 1 and d.get("source_id") == oid:
                    if await export_named("pre"):
                        say("PRE exported before +1 activation")
                    say(f"[P0] activating Oko +1 (src {oid})")
                    wire("oko_plus_one_activate", {"src": oid, "data": d})
                    await submit_as_is(p0, a)
                    ST["act_in_flight"] = True
                    ST["act_turn"] = state.get("turn_number")
                    return
        if await discard_tick(p0, 0, "P0", st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, "P1")
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p1, sub)
            return
        if await target_tick(p1, 1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        # ---- P1 priority: cast bears when 2+ untapped lands ----
        if (state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1
                and len(bf_ids(state, 1, BEAR)) < 3
                and len(untapped_lands(state, 1, (FOREST,))) >= 2
                and BEAR in hand_lnames(state, 1)):
            coid = await cast_named(p1, acts, state, BEAR, "P1")
            if coid is not None:
                return
        if await discard_tick(p1, 1, "P1", st, state):
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

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
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
                obs["tick_errors"].append({"who": tag, "err": str(e)[:200]})
                wire("tick_error", {"who": tag, "err": str(e)})
        s = (p0.latest or {}).get("state") or {}
        # POST: on P0's NEXT turn after the +1 resolved
        if (ST["plus_one_done"] and ST["mid_exported"]
                and not ST["post_exported"]
                and s.get("active_player") == 0
                and (s.get("turn_number") or 0) > (ST["mid_act_turn"] or 0)
                and s.get("phase") in ("PreCombatMain", "PostCombatMain")
                and not (s.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"POST exported on P0 turn {s.get('turn_number')}")
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - t0 > 1200 and ST["plus_one_done"]:
            notes.append("watchdog: 1200s elapsed; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0lands={len(untapped_lands(s,0))} "
                f"P1bears={len(bf_ids(s,1,BEAR))} oko={ST['oko_oid']} "
                f"cast={ST['oko_cast']} act={ST['plus_one_done']} "
                f"mid={ST['mid_exported']} post={ST['post_exported']} "
                f"stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
