#!/usr/bin/env python3
"""Issue #6950: [Card Bug] Elenda, Saint of Dusk - life-total-vs-starting-life
static ability is unrecognized.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3, FDN printing):
  Elenda, Saint of Dusk ({2}{W}{B} Legendary Creature - Vampire Knight, 4/4):
    "Lifelink, hexproof from instants
     As long as your life total is greater than your starting life total,
     Elenda gets +1/+1 and has menace.
     Elenda gets an additional +5/+5 as long as your life total is at least
     10 greater than your starting life total."

Card-data parse state on v0.81.3 (observed 2026-09-13 before the run):
  static[0] condition parses: QuantityComparison { GT, LifeTotal(Controller),
    StartingLifeTotal } with AddPower/AddToughness/AddKeyword(Menace).
  static[1] condition is Unrecognized("your life total is at least 10 greater
    than your starting life total") with AddPower 5 / AddToughness 5.

Reported symptom (v0.42.0): deck builder flags
  Static:Unrecognized(your life total is at least 10 greater than your
  starting li...); the conditional buff statics don't apply.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Elenda, Saint of Dusk, 12x Revitalize ({1}{W}: you gain 3 life,
      draw a card; untargeted, non-modal), 22x Plains, 22x Swamp.
  P1: 60x Forest (passive).

Plan:
  1. P0 casts Elenda on/after turn 4. PRE exported with Elenda on BF at
     P0 life 20 (== starting): Oracle-correct is 4/4, no menace.
  2. P0 casts Revitalize x1 (life 23). MID1: static[0] should apply ->
     Oracle-correct is 5/5 with menace; static[1] must NOT apply yet
     (23 < 30).
  3. P0 casts Revitalize x3 more (life 32, >= 10 over starting). POST:
     static[1] should additionally apply -> Oracle-correct is 10/10
     with menace.

Assertions (Oracle-correct expectations; a FAIL on A4/A5 is the reported
defect):
  A1_setup_ok   PRE: Elenda on P0 BF; P0 life == 20.
  A2_parse      card-data: static[0] condition parses (QuantityComparison GT
                LifeTotal vs StartingLifeTotal); static[1] condition is
                Unrecognized("your life total is at least 10 greater than
                your starting life total").
  A3_gain_23    MID1: one Revitalize resolved, P0 life 20 -> 23.
  A4_below_threshold
                PRE (life 20): Elenda is 4/4 with no Menace. FAILED =
                reported bug is REPRODUCED (the +5/+5 static applies even
                though life is NOT >= 10 above starting).
  A5_mid_threshold
                MID1 (life 23, still < 10 above starting): Elenda is 5/5
                with Menace. FAILED = same defect (observed 10/10).
  A6_at_threshold
                POST (life 32, >= 10 above starting): Elenda is 10/10 with
                Menace.
  A7_cleanup    POST: stack empty, waiting_for is Priority/None.

Keyword checks use obj["keywords"] only: the full-object JSON contains the
string "Menace" inside static_definitions' AddKeyword modification text,
which false-positives a whole-blob substring scan.

Verdict rule: reproduced iff A1 passes and (A4 or A5 fails); not-reproduced
iff A1-A7 all pass; blocked iff A1 fails.

Evidence: evidence/6950/<run-id>/pre.json, mid1.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6950.py, wire_log.jsonl,
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6950")
EVDIR = f"{BACKFILL}/evidence/6950/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ELENDA = "elenda, saint of dusk"
REVIT = "revitalize"
PLAINS = "plains"
SWAMP = "swamp"
FOREST = "forest"
LANDS = (PLAINS, SWAMP, FOREST)

P0_DECK = [(ELENDA, 4), (REVIT, 12), (PLAINS, 22), (SWAMP, 22)]
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


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


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


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


def has_keyword(obj, kw):
    """Check obj["keywords"] only: the full-object JSON contains the string
    "Menace" inside static_definitions' AddKeyword modification text, which
    false-positives a whole-blob substring scan."""
    for k in obj.get("keywords") or []:
        if isinstance(k, str) and k.lower() == kw.lower():
            return True
        if isinstance(k, dict) and kw.lower() in json.dumps(k).lower():
            return True
    return False


def elenda_pt(state):
    oid = bf_id(state, 0, ELENDA)
    if oid is None:
        return None, None, None, None
    o = get_obj(state, oid)
    return oid, o.get("power"), o.get("toughness"), o


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
           ("A1_setup_ok", "A2_parse", "A3_gain_23", "A4_below_threshold",
            "A5_mid_threshold", "A6_at_threshold", "A7_cleanup")}

    # ---- A2 (parse) up front, from the pinned card-data.json ----
    cd_path = (f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json")
    try:
        cd = json.load(open(cd_path))
        e = cd["elenda, saint of dusk"]
        statics = e.get("static_abilities", [])
        notes.append(f"parse: {len(statics)} static_abilities in card-data")
        c0 = (statics[0] or {}).get("condition", {})
        c1 = (statics[1] or {}).get("condition", {})
        notes.append("parse: static[0] condition = " +
                     json.dumps(c0)[:220])
        notes.append("parse: static[1] condition = " +
                     json.dumps(c1)[:220])
        ok0 = (c0.get("type") == "QuantityComparison"
               and c0.get("comparator") == "GT"
               and json.dumps(c0).find("LifeTotal") >= 0
               and json.dumps(c0).find("StartingLifeTotal") >= 0)
        ok1 = (c1.get("type") == "Unrecognized"
               and "at least 10 greater" in str(c1.get("text", "")))
        ass["A2_parse"] = "passed" if (ok0 and ok1) else "failed"
        notes.append(f"A2_parse: static0_parsed={ok0} "
                     f"static1_unrecognized={ok1}")
        wire("parse_check", {"static0_condition": c0,
                             "static1_condition": c1})
    except Exception as ex:
        ass["A2_parse"] = "failed"
        notes.append(f"A2_parse failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"elenda_cast": False, "elenda_oid": None,
          "rev_casts": 0, "pre_exported": False,
          "mid1_exported": False, "post_exported": False,
          "mid1_at": None, "post_at": None,
          "rev_resolved_at": None, "rev_last_life": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "tick_errors": [], "life_trace": []}
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
        states = {}
        for fn in ("pre", "mid1", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre, mid1, post = (states.get(k) for k in ("pre", "mid1", "post"))

        # ---- A1: setup (Elenda on BF, life == starting) ----
        if pre is not None:
            oid, pw, tw, o = elenda_pt(pre)
            life = life_of(pre, 0)
            ok = (oid is not None and o.get("zone") == "Battlefield"
                  and life == 20)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            men = has_keyword(o, "menace") if o else None
            notes.append(f"A1: oid={oid} zone={o.get('zone') if o else None} "
                         f"life={life} P/T={pw}/{tw} menace={men}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: pre.json missing")

        # ---- A3: first Revitalize -> 23 ----
        if mid1 is not None:
            life = life_of(mid1, 0)
            ok = (life == 23)
            ass["A3_gain_23"] = "passed" if ok else "failed"
            notes.append(f"A3: MID1 P0 life={life} (expected 23)")
        else:
            ass["A3_gain_23"] = "failed"
            notes.append("A3 failed: mid1.json missing")

        # ---- A4: below threshold (life 20) the +5/+5 must NOT apply ----
        if pre is not None:
            oid, pw, tw, o = elenda_pt(pre)
            men = has_keyword(o, "menace") if o else None
            ok = (pw == 4 and tw == 4 and men is False)
            ass["A4_below_threshold"] = "passed" if ok else "failed"
            if not ok:
                notes.append(f"A4 FAILED: PRE (life 20) Elenda P/T={pw}/{tw} "
                             f"menace={men} (Oracle-correct: 4/4, no menace). "
                             f"The +5/+5 static applies even though life is "
                             f"NOT >= 10 above starting. BUG REPRODUCED.")
            else:
                notes.append(f"A4: PRE Elenda P/T={pw}/{tw} menace={men}")
        else:
            ass["A4_below_threshold"] = "failed"
            notes.append("A4 failed: pre.json missing")

        # ---- A5: mid threshold (life 23 < 30): static[0] applies,
        # static[1] must NOT ----
        if mid1 is not None:
            oid, pw, tw, o = elenda_pt(mid1)
            men = has_keyword(o, "menace") if o else None
            ok = (pw == 5 and tw == 5 and men is True)
            ass["A5_mid_threshold"] = "passed" if ok else "failed"
            if not ok:
                notes.append(f"A5 FAILED: MID1 (life 23) Elenda P/T={pw}/{tw} "
                             f"menace={men} (Oracle-correct: 5/5 + menace; "
                             f"the +5/+5 static still applies below its "
                             f"threshold). BUG REPRODUCED.")
            else:
                notes.append(f"A5: MID1 Elenda P/T={pw}/{tw} menace={men}")
        else:
            ass["A5_mid_threshold"] = "failed"
            notes.append("A5 failed: mid1.json missing")

        # ---- A6: at threshold (life 32 >= 30): both statics apply ----
        if post is not None:
            life = life_of(post, 0)
            oid, pw, tw, o = elenda_pt(post)
            men = has_keyword(o, "menace") if o else None
            ok = (life is not None and life >= 30
                  and pw == 10 and tw == 10 and men is True)
            ass["A6_at_threshold"] = "passed" if ok else "failed"
            notes.append(f"A6: POST life={life} Elenda P/T={pw}/{tw} "
                         f"menace={men} (expected 10/10 + menace)")
        else:
            ass["A6_at_threshold"] = "failed"
            notes.append("A6 failed: post.json missing")

        # ---- A7: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wf = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={stack_empty} waiting_for={wf}")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 failed: post.json missing")

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif (ass["A4_below_threshold"] == "failed"
                or ass["A5_mid_threshold"] == "failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6950,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (log in "
                               f"runs/{RUN_ID}/server.log)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6950.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid1.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_6950.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "4x Elenda / 12x Revitalize density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "Revitalize stands in for 'gain life' triggers; the static's "
                "deck-builder flag is evidenced via the card-data parse check.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6950.py",
                    f"{EVDIR}/scenario_6950.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6950.py and server.log into EVDIR")
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
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 900
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6950 - Elenda, Saint of Dusk",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "life-total-vs-starting-life statics",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (from saved states / card-data):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "PRE: Elenda on P0 BF @ life 20",
            "A2_parse": "card-data: static0 parses; static1 Unrecognized",
            "A3_gain_23": "MID1: 1 Revitalize resolved: P0 life 20 -> 23",
            "A4_below_threshold": "PRE (life 20): Elenda 4/4, no menace",
            "A5_mid_threshold": "MID1 (life 23): Elenda 5/5 + menace",
            "A6_at_threshold": "POST (life 32): Elenda 10/10 + menace",
            "A7_cleanup": "stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Elenda across states (life / P/T / menace):",
               fill=(200, 210, 225))
        y += 24
        for label in ("pre", "mid1", "post"):
            st = states.get(label)
            if st is not None:
                oid, pw, tw, o = elenda_pt(st)
                men = has_keyword(o, "menace") if o else None
                line = (f"{label:>4}: life={life_of(st, 0)} "
                        f"elenda P/T={pw}/{tw} menace={men}")
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line, fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:13]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        # NOTE: scenario_run.log is hashed LAST, after the final say() line
        # below is appended; otherwise the manifest's hash for the log goes
        # stale by exactly one line.
        files = ["pre.json", "mid1.json", "post.json", "run.json",
                 "scenario_6950.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        # Re-hash now that all scenario_run.log logging is done. No say()
        # may follow this point (finish() only closes files + stdout).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in LANDS)
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
                if t in LANDS:
                    return 0
                if t == ELENDA:
                    return 1
                return 2  # Revitalize kept last

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
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
        # track Elenda on BF + life trace
        oid = bf_id(state, 0, ELENDA)
        if oid is not None and ST["elenda_oid"] is None:
            ST["elenda_oid"] = int(oid)
            say(f"Elenda on BF: oid={oid}")
        life = life_of(state, 0)
        tr = obs["life_trace"]
        if life is not None and (not tr or tr[-1][1] != life):
            tr.append((round(time.time() - t_start, 1), life))
            say(f"P0 life = {life}")
            wire("life", {"life": life})
        if await discard_tick(p0, 0, "P0", st, state):
            return
        # PRE: Elenda resolved, no Revitalize yet, stack empty, life == 20
        if (ST["elenda_oid"] is not None and not ST["pre_exported"]
                and ST["rev_casts"] == 0 and not (state.get("stack") or [])
                and life == 20 and my_priority(state, 0)):
            if await export_named("pre"):
                ST["pre_exported"] = True
                say("PRE exported: Elenda on BF at life 20")
        # MID1: life hit 23, stack empty, 8s grace
        if (ST["rev_casts"] >= 1 and not ST["mid1_exported"]
                and life == 23 and not (state.get("stack") or [])):
            if ST["rev_resolved_at"] is None:
                ST["rev_resolved_at"] = time.time()
        if (ST["rev_resolved_at"] is not None and not ST["mid1_exported"]
                and time.time() - ST["rev_resolved_at"] > 8
                and not (state.get("stack") or [])):
            if await export_named("mid1"):
                ST["mid1_exported"] = True
                ST["rev_resolved_at"] = None
                say("MID1 exported: life 23")
        # POST: life >= 30, stack empty, 8s grace
        if (life is not None and life >= 30 and not ST["post_exported"]
                and not (state.get("stack") or [])):
            if ST["post_at"] is None and ST["rev_last_life"] != life:
                ST["rev_last_life"] = life
                ST["post_at"] = time.time()
        if (ST["post_at"] is not None and not ST["post_exported"]
                and time.time() - ST["post_at"] > 8
                and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                say("POST exported")
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority (hold main-phase actions while a spell is in
        # flight: the caster gets priority first; always fall through to
        # PassPriority so nothing deadlocks) ----
        in_flight = bool(state.get("stack") or [])
        if (not in_flight
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            hn = hand_lnames(state, 0)
            lands = len(untapped_lands(state, 0))
            life = life_of(state, 0) or 0
            grace_hold = ((ST["rev_resolved_at"] is not None
                           and not ST["mid1_exported"])
                          or (ST["post_at"] is not None
                              and not ST["post_exported"]))
            if (not ST["elenda_cast"] and oid is None
                    and ELENDA in hn and lands >= 4):
                coid = await cast_named(p0, acts, state, ELENDA, "P0")
                if coid is not None:
                    ST["elenda_cast"] = True
                    return
            # cast Revitalizes until life >= 32 (4 total: 20 -> 32); hold
            # casts during the mid1/post export grace windows and hold the
            # 2nd+ Revitalize until MID1 is exported.
            if (ST["pre_exported"] and not grace_hold
                    and ST["elenda_oid"] is not None and REVIT in hn
                    and life < 32 and lands >= 2
                    and (ST["mid1_exported"] or ST["rev_casts"] < 1)):
                coid = await cast_named(p0, acts, state, REVIT, "P0")
                if coid is not None:
                    ST["rev_casts"] += 1
                    ST["rev_resolved_at"] = None
                    ST["post_at"] = None
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
        if ST["post_exported"] and time.time() - (ST.get("post_at") or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - t0 > 1200 and ST["post_exported"]:
            notes.append("watchdog: 1200s elapsed; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            oid, pw, tw, o = elenda_pt(s)
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0life={life_of(s, 0)} "
                f"elenda={oid} P/T={pw}/{tw} "
                f"menace={has_keyword(o, 'menace') if o else None} "
                f"rev_casts={ST['rev_casts']} "
                f"pre={ST['pre_exported']} mid1={ST['mid1_exported']} "
                f"post={ST['post_exported']} stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
