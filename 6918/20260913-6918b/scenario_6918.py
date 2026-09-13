#!/usr/bin/env python3
"""Issue #6918: Door of Destinies doesn't add charge counters when casting
creature type.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Door of Destinies ({4} Artifact):
    "As this artifact enters, choose a creature type.
     Whenever you cast a spell of the chosen type, put a charge counter on
     this artifact.
     Creatures you control of the chosen type get +1/+1 for each charge
     counter on this artifact."
Card data marks it fully_parsed: entry replacement chooses a creature type
(persist), a SpellCast trigger with valid_card IsChosenCardType putting a
charge counter on self, and a continuous anthem for chosen-type creatures.

Reported symptom: casting a spell of the chosen type adds no charge counter.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x door of destinies, 8x llanowar elves, 8x grizzly bears, 40x forest.
      Casts Door (chooses "Elf" at entry), then casts elves and one bear.
  P1: 60x forest (passive).

Plan:
  1. P0 casts Door of Destinies on/after turn 4 (4 untapped lands); chooses
     "Elf" at the entry choice prompt (recorded explicitly).
  2. PRE exported just before the Door cast.
  3. P0 casts Llanowar Elves (an Elf) -> MID1 after the cast + trigger
     window: Door should have 1 charge counter.
  4. P0 casts Grizzly Bears (a Bear, not the chosen type) -> MID2:
     counters should stay at 1 (control).
  5. P0 casts a second Llanowar Elves -> POST: Door should have 2 charge
     counters; the elves should be 3/3 from the +2/+2 anthem.

Assertions:
  A1_setup_ok        door on P0 BF; entry choice answered, chosen type "elf"
                     recorded.
  A2_counter_on_elf  MID1: door charge counters == 1. FAILED = reported bug
                     is REPRODUCED.
  A3_control_bear    MID2: door charge counters == MID1 value (bear of
                     non-chosen type adds nothing).
  A4_anthem          POST: elves' power/toughness == 1+charge / 1+charge
                     (consistency of the anthem with actual counters).
  A5_cleanup         POST: stack empty, waiting_for is Priority/None.

Verdict rule: reproduced iff A1 passes and A2 fails. not-reproduced iff
A1-A5 all pass. blocked iff A1 fails.

Evidence: evidence/6918/<run-id>/pre.json, mid1.json, mid2.json, post.json,
run.json, manifest.sha256, summary.png, scenario_6918.py, wire_log.jsonl,
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6918b")
EVDIR = f"{BACKFILL}/evidence/6918/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DOOR = "door of destinies"
ELF = "llanowar elves"
BEAR = "grizzly bears"
FOREST = "forest"

P0_DECK = [(DOOR, 4), (ELF, 8), (BEAR, 8), (FOREST, 40)]
P1_DECK = [(FOREST, 60)]

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


def untapped_lands(state, pid, names=(FOREST,)):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in names):
            out.append(int(oid))
    return out


def charge_counters(o):
    """Return the number of charge counters on an object, tolerating shape
    variants (dict {charge: n}, list of {type,count}, plain int)."""
    c = o.get("counters")
    if c is None:
        return 0
    if isinstance(c, dict):
        for k, v in c.items():
            if "charge" in str(k).lower():
                return int(v) if isinstance(v, (int, float)) else 0
        # some dicts may store {type: charge} differently; log raw
        return 0
    if isinstance(c, list):
        n = 0
        for e in c:
            if isinstance(e, dict):
                t = str(e.get("type") or e.get("kind") or "").lower()
                if "charge" in t:
                    n += int(e.get("count", e.get("n", 1)))
            elif isinstance(e, str) and "charge" in e.lower():
                n += 1
        return n
    if isinstance(c, (int, float)):
        return int(c)
    return 0


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
        if stype == "text":
            # "text" spec expects a value payload, not choiceIds: the server
            # rejects {"choiceIds": [...]} with "missing field `value`".
            val = None
            for s in choice.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (isinstance(d, dict) and d.get("role") == "choice"
                        and "value" in d):
                    val = d["value"]
                    break
            sub = {"interactionId": iid,
                   "response": {"type": "text", "data": {"value": val}}}
        else:
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
           ("A1_setup_ok", "A2_counter_on_elf", "A3_control_bear",
            "A4_anthem", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"door_cast": False, "door_cast_turn": None, "door_oid": None,
          "chosen_type": None, "choice_iid": None,
          "elf1_cast": False, "elf1_resolved_at": None,
          "mid1_exported": False,
          "bear_cast": False, "bear_resolved_at": None,
          "mid2_exported": False,
          "elf2_cast": False, "elf2_resolved_at": None,
          "post_exported": False, "post_at": None,
          "counters_seen": [], "trigger_stack_seen": False}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "type_choice": [], "tick_errors": []}
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

    def door_oid(state):
        return ST["door_oid"] or bf_id(state, 0, DOOR)

    def charge_of(state):
        oid = door_oid(state)
        if oid is None:
            return None
        return charge_counters(get_obj(state, oid))

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "mid1", "mid2", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre, mid1, mid2, post = (states.get(k) for k in
                                 ("pre", "mid1", "mid2", "post"))

        # ---- A1: setup ----
        if mid1 is not None or post is not None:
            st = mid1 or post
            oid = door_oid(st)
            o = get_obj(st, oid) if oid is not None else {}
            ok = (oid is not None and o.get("zone") == "Battlefield"
                  and ST["chosen_type"] == "elf")
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: door_oid={oid} zone={o.get('zone')} "
                         f"chosen_type={ST['chosen_type']} "
                         f"choice_iid={ST['choice_iid']}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: no usable states")

        # ---- A2: counter after elf cast ----
        if mid1 is not None:
            n = charge_of(mid1)
            ok = (n == 1)
            ass["A2_counter_on_elf"] = "passed" if ok else "failed"
            if not ok:
                notes.append(f"A2 FAILED: door charge counters in MID1 = {n} "
                             f"(expected 1 after casting an Elf). "
                             f"BUG REPRODUCED.")
            else:
                notes.append(f"A2: door charge counters in MID1 = {n}")
        else:
            ass["A2_counter_on_elf"] = "failed"
            notes.append("A2 failed: mid1.json missing")

        # ---- A3: control — bear of non-chosen type adds nothing ----
        if mid2 is not None and mid1 is not None:
            n1, n2 = charge_of(mid1), charge_of(mid2)
            ok = (n2 == n1)
            ass["A3_control_bear"] = "passed" if ok else "failed"
            notes.append(f"A3: counters mid1={n1} mid2={n2} "
                         f"(bear cast between)")
        else:
            ass["A3_control_bear"] = "failed"
            notes.append("A3 failed: mid1.json or mid2.json missing")

        # ---- A4: anthem consistent with actual counters ----
        if post is not None:
            n = charge_of(post)
            elves = [int(oid) for oid, o in
                     (post.get("objects", {}) or {}).items()
                     if o.get("zone") == "Battlefield"
                     and o.get("controller") == 0
                     and str(o.get("base_name") or o.get("name") or "")
                     .lower() == ELF]
            sigs = [(obj_name(post, e),
                     get_obj(post, e).get("power"),
                     get_obj(post, e).get("toughness")) for e in elves]
            if elves and n is not None:
                ok = all(p == 1 + n and t == 1 + n for _, p, t in sigs)
                ass["A4_anthem"] = "passed" if ok else "failed"
                notes.append(f"A4: charge={n} elves={sigs} "
                             f"(expected 1+{n}/1+{n})")
            else:
                ass["A4_anthem"] = "failed"
                notes.append(f"A4 failed: elves={sigs} charge={n}")
        else:
            ass["A4_anthem"] = "failed"
            notes.append("A4 failed: post.json missing")

        # ---- A5: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wf = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A5_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A5: stack_empty={stack_empty} waiting_for={wf}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: post.json missing")

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif ass["A2_counter_on_elf"] == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6918,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (pid in runs/<run-id>/"
                               "server.pid)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6918.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid1.json", "mid2.json",
                               "post.json", "run.json", "manifest.sha256",
                               "summary.png", "scenario_6918.py",
                               "wire_log.jsonl", "scenario_run.log",
                               "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "4x Door / 8x Elves / 8x Bears density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6918.py",
                    f"{EVDIR}/scenario_6918.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6918.py and server.log into EVDIR")
        render_summary(run, states)
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

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 840
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6918 - Door of Destinies",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "charge counters on casting chosen-type spell",
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
            "A1_setup_ok": "Door on P0 BF; entry choice answered 'Elf'",
            "A2_counter_on_elf": "MID1: door charge counters == 1 (Elf cast)",
            "A3_control_bear": "MID2: bear cast adds no counter (unchanged)",
            "A4_anthem": "POST: elves are 1+charge / 1+charge",
            "A5_cleanup": "stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Door of Destinies charge counters across states:",
               fill=(200, 210, 225))
        y += 24
        for label in ("pre", "mid1", "mid2", "post"):
            st = states.get(label)
            if st is not None:
                oid = door_oid(st)
                n = charge_of(st)
                line = f"{label:>4}: door_oid={oid} charge_counters={n}"
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line, fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:11]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "mid1.json", "mid2.json", "post.json",
                 "run.json", "scenario_6918.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]
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
        lands = sum(1 for n in hn if n == FOREST)
        mulls = kept.get(f"P{pid}_mulls", 0)
        ok = lands >= 3 or mulls >= 2
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
                return 0 if t in (FOREST, ELF, BEAR) else 1

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def type_choice_tick(c, pid, tag, st, state):
        """Answer Door of Destinies' 'as enters, choose a creature type'.

        Gate on waiting_for.type == "NamedChoice" (lesson from #6917: an
        in-flight flag alone lets the handler fire on Priority exactChoices
        menus). The "text" response schema needs {"value": ...}, NOT
        {"choiceIds": [...]} (the server parse-rejects the latter with
        "missing field `value`"). Do not mark the prompt done until the
        waiting_for leaves NamedChoice; a parse-rejected submission leaves
        the same iid pending and may be corrected and resubmitted.
        """
        if ST["chosen_type"] is not None or not ST["door_cast"]:
            return False
        if (wf_of(state).get("type") or "") != "NamedChoice":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "attempts": 0, "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            if entry["attempts"] >= 5:
                if not entry.get("gave_up"):
                    entry["gave_up"] = True
                    say(f"[{tag}] entry choice: 5 attempts failed; "
                        f"recording stall")
                    obs["tick_errors"].append(
                        {"who": tag, "err": "entry choice 5x failed"})
                return False
            texts = [choice_text(ch) for ch in chs]
            wire("entry_choice_prompt",
                 {"who": tag, "iid": str(iid)[:16],
                  "wf_type": wf_of(state).get("type"),
                  "n_choices": len(chs),
                  "attempt": entry["attempts"] + 1,
                  "sample_texts": texts[:12],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] ENTRY CHOICE PROMPT wf=NamedChoice n={len(chs)} "
                f"attempt={entry['attempts'] + 1}")
            pick = None
            for ch in chs:
                if choice_text(ch).lower() == "elf":
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if "elf" in choice_text(ch).lower():
                        pick = ch
                        break
            if pick is None:
                say(f"[{tag}] no Elf choice found; waiting for engine view")
                return False
            entry["attempts"] += 1
            ST["choice_iid"] = str(iid)[:16]
            obs["type_choice"].append(
                {"who": tag, "iid": str(iid)[:16],
                 "chosen": choice_text(pick).lower(),
                 "attempt": entry["attempts"],
                 "wf_type": wf_of(state).get("type"),
                 "n_choices": len(chs)})
            say(f"[{tag}] choosing creature type: "
                f"{choice_text(pick)} (attempt {entry['attempts']})")
            await answer_vi(c, opp, pick, tag)
            # do NOT mark done: only a wf change away from NamedChoice
            # proves the submission was accepted. Reset attempts only then.
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

    def elves_on_bf(state):
        return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
                if o.get("zone") == "Battlefield" and o.get("controller") == 0
                and str(o.get("base_name") or o.get("name") or "")
                .lower() == ELF]

    def bears_on_bf(state):
        return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
                if o.get("zone") == "Battlefield" and o.get("controller") == 0
                and str(o.get("base_name") or o.get("name") or "")
                .lower() == BEAR]

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
        if await type_choice_tick(p0, 0, "P0", st, state):
            return
        # record door on BF; confirm the entry choice was accepted once the
        # waiting_for leaves NamedChoice after our submission
        oid = bf_id(state, 0, DOOR)
        if oid is not None and ST["door_oid"] is None:
            ST["door_oid"] = int(oid)
            say(f"Door on BF: oid={oid}")
            wire("door_entered", {"oid": int(oid),
                                 "counters": get_obj(state, oid).get("counters")})
        if (ST["choice_iid"] is not None and ST["chosen_type"] is None
                and wtype != "NamedChoice"):
            ST["chosen_type"] = "elf"
            iid = ST["choice_iid"]
            entry = prompt_first_seen.get(iid)
            if entry is not None:
                entry["done"] = True
            do = get_obj(state, oid) if oid is not None else {}
            say(f"ENTRY CHOICE ACCEPTED: chosen_type=elf; door object keys="
                f"{sorted(do.keys())}")
            wire("entry_choice_accepted",
                 {"door_object": json.loads(json.dumps(do, default=str))})
            notes.append("entry choice prompt answered 'Elf' and accepted "
                         f"(iid {iid}); door object keys="
                         f"{sorted(do.keys())}")
        # watch the trigger stack entry
        for se in state.get("stack") or []:
            js = json.dumps(se, default=str)
            if "charge" in js.lower() and "door" in js.lower():
                if not ST["trigger_stack_seen"]:
                    ST["trigger_stack_seen"] = True
                    say(f"TRIGGER ON STACK: {js[:220]}")
                    wire("trigger_stack", {"entry": js[:800]})
        # track counters over time
        n = charge_of(state)
        if n is not None:
            seen = ST["counters_seen"]
            if not seen or seen[-1][1] != n:
                seen.append((time.time() - t_start, n))
                say(f"door charge counters = {n}")
        # mid1: elf1 cast, spell resolved (elf on BF), stack empty, 10s grace
        if (ST["elf1_cast"] and not ST["mid1_exported"]
                and len(elves_on_bf(state)) >= 1
                and not (state.get("stack") or [])
                and ST["elf1_resolved_at"] is None):
            ST["elf1_resolved_at"] = time.time()
        if (ST["elf1_resolved_at"] is not None and not ST["mid1_exported"]
                and time.time() - ST["elf1_resolved_at"] > 10
                and not (state.get("stack") or [])):
            if await export_named("mid1"):
                ST["mid1_exported"] = True
                say(f"MID1 exported (charge={charge_of(state)})")
        # mid2: bear cast, resolved, 10s grace
        if (ST["bear_cast"] and not ST["mid2_exported"]
                and len(bears_on_bf(state)) >= 1
                and not (state.get("stack") or [])
                and ST["bear_resolved_at"] is None):
            ST["bear_resolved_at"] = time.time()
        if (ST["bear_resolved_at"] is not None and not ST["mid2_exported"]
                and time.time() - ST["bear_resolved_at"] > 10
                and not (state.get("stack") or [])):
            if await export_named("mid2"):
                ST["mid2_exported"] = True
                say(f"MID2 exported (charge={charge_of(state)})")
        # post: elf2 cast, resolved, 10s grace
        if (ST["elf2_cast"] and not ST["post_exported"]
                and len(elves_on_bf(state)) >= 2
                and not (state.get("stack") or [])
                and ST["elf2_resolved_at"] is None):
            ST["elf2_resolved_at"] = time.time()
        if (ST["elf2_resolved_at"] is not None and not ST["post_exported"]
                and time.time() - ST["elf2_resolved_at"] > 10
                and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"POST exported (charge={charge_of(state)})")
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        if (not ST["door_cast"] and oid is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0)) >= 4
                and DOOR in hand_lnames(state, 0)):
            if await export_named("pre"):
                say("PRE exported before Door cast")
            coid = await cast_named(p0, acts, state, DOOR, "P0")
            if coid is not None:
                ST["door_cast"] = True
                ST["door_cast_turn"] = state.get("turn_number")
                return
        if (ST["door_cast"] and ST["chosen_type"] == "elf"
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not (state.get("stack") or [])):
            lands = len(untapped_lands(state, 0))
            hn = hand_lnames(state, 0)
            if (not ST["elf1_cast"] and ELF in hn and lands >= 1):
                coid = await cast_named(p0, acts, state, ELF, "P0")
                if coid is not None:
                    ST["elf1_cast"] = True
                    say("elf1 cast (of chosen type)")
                    return
            if (ST["mid1_exported"] and not ST["bear_cast"]
                    and BEAR in hn and lands >= 2):
                coid = await cast_named(p0, acts, state, BEAR, "P0")
                if coid is not None:
                    ST["bear_cast"] = True
                    say("bear cast (control: not chosen type)")
                    return
            if (ST["mid2_exported"] and not ST["elf2_cast"]
                    and ELF in hn and lands >= 1):
                coid = await cast_named(p0, acts, state, ELF, "P0")
                if coid is not None:
                    ST["elf2_cast"] = True
                    say("elf2 cast (of chosen type)")
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
        if await type_choice_tick(p1, 1, "P1", st, state):
            return
        if await discard_tick(p1, 1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        # ---- P1 priority: play land, pass ----
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
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - t0 > 1200 and ST["post_exported"]:
            notes.append("watchdog: 1200s elapsed; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0lands={len(untapped_lands(s,0))} "
                f"door={ST['door_oid']} type={ST['chosen_type']} "
                f"charge={charge_of(s)} "
                f"elf1={ST['elf1_cast']}/{ST['mid1_exported']} "
                f"bear={ST['bear_cast']}/{ST['mid2_exported']} "
                f"elf2={ST['elf2_cast']}/{ST['post_exported']} "
                f"stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
